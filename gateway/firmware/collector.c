/*
 * collector.c - state machine of the battery wM-Bus collector
 *
 *  SLEEP --RTC alarm--> RADIO_BOOT (ping) --> GET_CFG --> SET_CFG --> LISTEN
 *    ^                                                                   |
 *    |                            window timeout or all known meters seen
 *    |                                                                   v
 *    +--- (ESP32 off) <-- WAIT_DONE <-- SEND_RECORDS <-- ESP_BOOT <-- radio off
 */
#include "collector.h"

#include <string.h>

#include "hci.h"
#include "imst_wmbus.h"
#include "keystore.h"
#include "link_proto.h"
#include "oms.h"
#include "port.h"

/* ---- timing (ms) ---- */
#define PING_PERIOD_MS       200u     /* module boot time is not specified -> poll */
#define PING_TRIES           15u
#define RSP_TIMEOUT_MS       1000u    /* HCI spec p.4: response expected < 1 s     */
#define RSP_TRIES            3u
#define ESP_BOOT_TIMEOUT_MS  10000u
#define ACK_TIMEOUT_MS       30000u
#define SESSION_TIMEOUT_MS   180000u  /* hard cap for one LTE session              */

#define POOL_SIZE            (24u * 1024u)
#define CHUNK_MAX            480u     /* < HCI_MAX_PAYLOAD incl. seq               */

typedef enum {
    ST_SLEEP, ST_RADIO_BOOT, ST_GET_CFG, ST_SET_CFG, ST_LISTEN,
    ST_ESP_BOOT, ST_SEND_RECORDS, ST_WAIT_DONE
} state_t;

static struct {
    state_t         st;
    uint32_t        t_state, t_tx, t_session;
    uint8_t         tries;
    collector_cfg_t cfg;
    uint8_t         imst_cfg[IMST_CFG_LEN];
    bool            got_rsp;
    uint8_t         rsp_status;

    uint8_t         seen[(KS_MAX_METERS + 7u) / 8u];
    uint8_t         failed[(KS_MAX_METERS + 7u) / 8u];
    uint16_t        n_seen, n_unknown, n_bad, n_rec;

    uint8_t         pool[POOL_SIZE];  /* records, kept until every chunk is acked */
    size_t          pool_n;
    size_t          send_pos, chunk_end;
    uint16_t        seq;
    bool            acked, done, keys_dirty, alarm;

    hci_rx_t        radio_rx, link_rx;
    uint8_t         tx[HCI_ENCODED_MAX(HCI_MAX_PAYLOAD)];
} s;

/* ------------------------------------------------------------------ helpers */
static bool bit_get(const uint8_t *b, size_t i) { return (b[i >> 3] >> (i & 7u)) & 1u; }
static void bit_set(uint8_t *b, size_t i)       { b[i >> 3] |= (uint8_t)(1u << (i & 7u)); }

static void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, (uint16_t)v); put16(p + 2, (uint16_t)(v >> 16)); }
static uint16_t get16(const uint8_t *p)   { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t get32(const uint8_t *p)   { return get16(p) | ((uint32_t)get16(p + 2) << 16); }

static void enter(state_t st) { s.st = st; s.t_state = port_millis(); s.tries = 0; }

static void radio_tx(size_t n) { if (n) { port_radio_send(s.tx, n); s.t_tx = port_millis(); } }

static void link_tx(uint8_t msg, const uint8_t *p, uint16_t len)
{
    size_t n = hci_encode(LINK_SAP, msg, p, len, s.tx, sizeof s.tx);
    if (n) port_link_send(s.tx, n);
}

static void schedule_next_window(void)
{
    uint32_t now = port_utc();
    uint32_t day0 = now - (now % 86400u);
    uint32_t period = s.cfg.period_s < 300u ? 300u : s.cfg.period_s;  /* guard */
    uint32_t next = day0 + (s.cfg.offset_s % 86400u);
    while (next <= now) next += period;
    port_rtc_alarm_at(next);
}

static void pool_add(uint32_t utc, int8_t rssi, uint8_t status, const uint8_t *pkt, size_t len)
{
    if (s.pool_n + 6u + len > sizeof s.pool) return;          /* full: drop */
    uint8_t *r = &s.pool[s.pool_n];
    put32(r, utc);
    r[4] = (uint8_t)rssi;
    r[5] = status;
    memcpy(&r[6], pkt, len);
    s.pool_n += 6u + len;
    s.n_rec++;
}

/* ------------------------------------------------------- radio (iM891A-XL) */
static void on_rx_telegram(const imst_rx_t *rx)
{
    uint8_t id[6];
    oms_meter_id(rx->pkt, id);
    int idx = ks_index_of(id);
    if (idx < 0) { s.n_unknown++; return; }                   /* not our meter */
    if (bit_get(s.seen, (size_t)idx)) return;                 /* one per window */

    uint8_t buf[256];
    memcpy(buf, rx->pkt, rx->pkt_len);
    oms_result_t r = oms_decrypt_inplace(buf, rx->pkt_len, ks_at((size_t)idx)->key);

    if (r == OMS_DECRYPTED || r == OMS_PLAIN) {
        pool_add(port_utc(), rx->rssi_dbm, (uint8_t)r, buf, rx->pkt_len);
        bit_set(s.seen, (size_t)idx);
        s.n_seen++;
    } else if (!bit_get(s.failed, (size_t)idx)) {
        /* wrong key / unsupported: forward the original once so the server knows */
        pool_add(port_utc(), rx->rssi_dbm, (uint8_t)r, rx->pkt, rx->pkt_len);
        bit_set(s.failed, (size_t)idx);
        s.n_bad++;
    }
}

static void radio_cb(const hci_msg_t *m, void *ctx)
{
    (void)ctx;
    if (m->sap == IMST_SAP_DEVMGMT && m->msg == IMST_DM_PING_RSP) {
        s.got_rsp = true;
        s.rsp_status = m->len ? m->payload[0] : 0xFFu;
    } else if (m->sap == IMST_SAP_WMBUS && m->msg == IMST_WM_GET_ACTIVE_CFG_RSP) {
        s.got_rsp = true;
        s.rsp_status = m->len ? m->payload[0] : 0xFFu;
        if (m->len >= 1u + IMST_CFG_LEN) memcpy(s.imst_cfg, &m->payload[1], IMST_CFG_LEN);
        else s.rsp_status = 0xFFu;
    } else if (m->sap == IMST_SAP_WMBUS && m->msg == IMST_WM_SET_ACTIVE_CFG_RSP) {
        s.got_rsp = true;
        s.rsp_status = m->len ? m->payload[0] : 0xFFu;
    } else if (m->sap == IMST_SAP_WMBUS && m->msg == IMST_WM_RX_PACKET_IND) {
        imst_rx_t rx;
        if (s.st == ST_LISTEN && imst_parse_rx(m, &rx)) on_rx_telegram(&rx);
    }
}

/* ----------------------------------------------------------- link (ESP32) */
static void link_cb(const hci_msg_t *m, void *ctx)
{
    (void)ctx;
    if (m->sap != LINK_SAP) return;
    const uint8_t *p = m->payload;
    switch (m->msg) {
    case LINK_DN_READY: s.got_rsp = true; break;
    case LINK_DN_ACK:   if (m->len >= 2u && get16(p) == s.seq) s.acked = true; break;
    case LINK_DN_TIME:  if (m->len >= 4u) port_set_utc(get32(p)); break;
    case LINK_DN_CFG:
        if (m->len >= 11u) {
            s.cfg.period_s  = get32(p);
            s.cfg.offset_s  = get32(p + 4);
            s.cfg.window_s  = get16(p + 8);
            s.cfg.link_mode = p[10];
            /* TODO: persist cfg in flash next to the key store */
        }
        break;
    case LINK_DN_KEY_PUT:
        for (uint8_t i = 0; m->len >= 1u && i < p[0] && 1u + (i + 1u) * 22u <= m->len; i++) {
            const uint8_t *e = &p[1u + i * 22u];
            if (ks_put(e, e + 6)) s.keys_dirty = true;
        }
        break;
    case LINK_DN_KEY_DEL:
        for (uint8_t i = 0; m->len >= 1u && i < p[0] && 1u + (i + 1u) * 6u <= m->len; i++)
            if (ks_del(&p[1u + i * 6u])) s.keys_dirty = true;
        break;
    case LINK_DN_DONE:  s.done = true; break;
    default: break;
    }
}

static bool send_next_chunk(void)
{
    if (s.send_pos >= s.pool_n) return false;
    uint8_t buf[2 + CHUNK_MAX];
    size_t n = 2, p = s.send_pos;
    while (p < s.pool_n) {
        size_t rec = 6u + (size_t)s.pool[p + 6u] + 1u;        /* 6 + (L + 1) */
        if (n + rec > sizeof buf) break;
        memcpy(&buf[n], &s.pool[p], rec);
        n += rec;
        p += rec;
    }
    s.seq++;
    put16(buf, s.seq);
    s.chunk_end = p;
    s.acked = false;
    link_tx(LINK_UP_RECORDS, buf, (uint16_t)n);
    s.t_tx = port_millis();
    return true;
}

/* --------------------------------------------------------------- public */
void collector_init(void)
{
    memset(&s, 0, sizeof s);
    s.cfg.period_s  = 86400u;
    s.cfg.offset_s  = 2u * 3600u;   /* 02:00 UTC, overwritten by LINK_DN_CFG */
    s.cfg.window_s  = 180u;
    s.cfg.link_mode = IMST_LINK_CT;
    hci_rx_init(&s.radio_rx, radio_cb, NULL);
    hci_rx_init(&s.link_rx, link_cb, NULL);
    ks_init();
    s.st = ST_SLEEP;
    schedule_next_window();
}

void collector_on_rtc_alarm(void) { s.alarm = true; }

bool collector_can_sleep(void) { return s.st == ST_SLEEP && !s.alarm; }

void collector_poll(void)
{
    uint8_t b;
    while (port_radio_getc(&b)) hci_rx_byte(&s.radio_rx, b);
    while (port_link_getc(&b))  hci_rx_byte(&s.link_rx, b);

    uint32_t now = port_millis();

    switch (s.st) {
    case ST_SLEEP:
        if (!s.alarm) break;
        s.alarm = false;
        memset(s.seen, 0, sizeof s.seen);
        memset(s.failed, 0, sizeof s.failed);
        s.n_seen = s.n_unknown = s.n_bad = s.n_rec = 0;   /* stats of this window */
        port_radio_power(true);
        hci_rx_init(&s.radio_rx, radio_cb, NULL);
        s.got_rsp = false;
        enter(ST_RADIO_BOOT);
        radio_tx(imst_build_ping(s.tx, sizeof s.tx));
        break;

    case ST_RADIO_BOOT:                       /* ping until the module answers */
        if (s.got_rsp) {
            s.got_rsp = false;
            enter(ST_GET_CFG);
            radio_tx(imst_build_get_active_cfg(s.tx, sizeof s.tx));
        } else if (now - s.t_tx >= PING_PERIOD_MS) {
            if (++s.tries >= PING_TRIES) { port_radio_power(false); enter(ST_SLEEP); schedule_next_window(); break; }
            radio_tx(imst_build_ping(s.tx, sizeof s.tx));
        }
        break;

    case ST_GET_CFG:
        if (s.got_rsp && s.rsp_status == IMST_STATUS_OK) {
            s.got_rsp = false;
            imst_cfg_patch(s.imst_cfg, s.cfg.link_mode);
            enter(ST_SET_CFG);
            radio_tx(imst_build_set_active_cfg(s.imst_cfg, s.tx, sizeof s.tx));
        } else if (s.got_rsp || now - s.t_tx >= RSP_TIMEOUT_MS) {
            s.got_rsp = false;
            if (++s.tries >= RSP_TRIES) { port_radio_power(false); enter(ST_SLEEP); schedule_next_window(); break; }
            radio_tx(imst_build_get_active_cfg(s.tx, sizeof s.tx));
        }
        break;

    case ST_SET_CFG:
        if (s.got_rsp && s.rsp_status == IMST_STATUS_OK) {
            s.got_rsp = false;
            enter(ST_LISTEN);                  /* Rx events arrive from now on */
        } else if (s.got_rsp || now - s.t_tx >= RSP_TIMEOUT_MS) {
            s.got_rsp = false;
            if (++s.tries >= RSP_TRIES) { port_radio_power(false); enter(ST_SLEEP); schedule_next_window(); break; }
            radio_tx(imst_build_set_active_cfg(s.imst_cfg, s.tx, sizeof s.tx));
        }
        break;

    case ST_LISTEN: {
        bool all_seen = ks_count() > 0u && s.n_seen >= ks_count();
        if (all_seen || now - s.t_state >= (uint32_t)s.cfg.window_s * 1000u) {
            port_radio_power(false);           /* gateway FW has no sleep command */
            port_esp_power(true);
            hci_rx_init(&s.link_rx, link_cb, NULL);
            s.got_rsp = false;
            s.done = false;
            s.t_session = now;
            enter(ST_ESP_BOOT);
        }
        break;
    }

    case ST_ESP_BOOT:
        if (s.got_rsp) {
            uint8_t h[8];
            put32(h, port_utc());
            put16(h + 4, port_battery_mv());
            put16(h + 6, (uint16_t)ks_count());
            link_tx(LINK_UP_HELLO, h, sizeof h);
            s.send_pos = 0;
            enter(ST_SEND_RECORDS);
            if (!send_next_chunk()) {          /* nothing collected: send END only */
                uint8_t e[10];
                put16(e, s.n_rec); put16(e + 2, s.n_seen); put16(e + 4, (uint16_t)ks_count());
                put16(e + 6, s.n_unknown); put16(e + 8, s.n_bad);
                link_tx(LINK_UP_END, e, sizeof e);
                enter(ST_WAIT_DONE);
            }
        } else if (now - s.t_state >= ESP_BOOT_TIMEOUT_MS) {
            port_esp_power(false); enter(ST_SLEEP); schedule_next_window();
        }
        break;

    case ST_SEND_RECORDS:
        if (s.acked) {
            s.send_pos = s.chunk_end;
            if (!send_next_chunk()) {
                uint8_t e[10];
                put16(e, s.n_rec); put16(e + 2, s.n_seen); put16(e + 4, (uint16_t)ks_count());
                put16(e + 6, s.n_unknown); put16(e + 8, s.n_bad);
                link_tx(LINK_UP_END, e, sizeof e);
                s.pool_n = 0;                  /* everything acknowledged */
                enter(ST_WAIT_DONE);
            }
        } else if (now - s.t_tx >= ACK_TIMEOUT_MS || now - s.t_session >= SESSION_TIMEOUT_MS) {
            /* keep unsent records in the pool, retry in the next session */
            if (s.send_pos > 0u) {
                memmove(s.pool, &s.pool[s.send_pos], s.pool_n - s.send_pos);
                s.pool_n -= s.send_pos;
            }
            port_esp_power(false); enter(ST_SLEEP); schedule_next_window();
        }
        break;

    case ST_WAIT_DONE:                         /* downlink: time, cfg, keys */
        if (s.done || now - s.t_session >= SESSION_TIMEOUT_MS) {
            if (s.keys_dirty) { ks_commit(); s.keys_dirty = false; }
            port_esp_power(false);
            enter(ST_SLEEP);
            schedule_next_window();
        }
        break;
    }
}
