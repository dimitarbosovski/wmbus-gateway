/* Simulated window: mock port + fake iM891A-XL + fake ESP32. Build: make -C test sim */
#include <stdio.h>
#include <string.h>

#include "../firmware/collector.h"
#include "../firmware/hci.h"
#include "../firmware/imst_wmbus.h"
#include "../firmware/keystore.h"
#include "../firmware/link_proto.h"
#include "../firmware/port.h"
#include "vectors.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); fails++; } \
                      else printf("ok   %s\n", #c); } while (0)

static uint32_t t_ms, t_utc = 1760000000u, alarm_at;
static bool radio_on, esp_on;
static uint8_t radio_q[4096], link_q[4096];
static size_t rq_h, rq_t, lq_h, lq_t;

/* ---- fake module: answers ping / get cfg / set cfg ---- */
static hci_rx_t mod_rx, esp_rx;
static int mod_cfg_set; static uint8_t mod_link_mode;
static void q_radio(const uint8_t *d, size_t n) { memcpy(&radio_q[rq_h], d, n); rq_h += n; }
static void q_link(const uint8_t *d, size_t n)  { memcpy(&link_q[lq_h], d, n); lq_h += n; }

static void mod_cb(const hci_msg_t *m, void *ctx)
{
    (void)ctx; uint8_t out[64], pl[12] = { 0 }; size_t n = 0;
    if (m->sap == IMST_SAP_DEVMGMT && m->msg == IMST_DM_PING_REQ)
        n = hci_encode(1, IMST_DM_PING_RSP, pl, 1, out, sizeof out);
    else if (m->sap == IMST_SAP_WMBUS && m->msg == IMST_WM_GET_ACTIVE_CFG_REQ) {
        pl[1 + IMST_CFG_OFS_LINK_MODE] = IMST_LINK_OFF;
        pl[1 + IMST_CFG_OFS_OPTIONS] = IMST_OPT_ADDR_FILTER;        /* must be cleared */
        pl[1 + IMST_CFG_OFS_LED_TIMING] = 50;
        pl[1 + IMST_CFG_OFS_RECAL_TIMEOUT] = 0xE8; pl[2 + IMST_CFG_OFS_RECAL_TIMEOUT] = 0x03;
        n = hci_encode(9, IMST_WM_GET_ACTIVE_CFG_RSP, pl, 12, out, sizeof out);
    } else if (m->sap == IMST_SAP_WMBUS && m->msg == IMST_WM_SET_ACTIVE_CFG_REQ) {
        uint16_t opt = (uint16_t)(m->payload[1] | (m->payload[2] << 8));
        mod_cfg_set = (m->len == IMST_CFG_LEN) && (opt & IMST_OPT_RX_NOTIFY) && !(opt & IMST_OPT_ADDR_FILTER)
                      && m->payload[7] == 0xE8;                    /* untouched field kept */
        mod_link_mode = m->payload[0];
        n = hci_encode(9, IMST_WM_SET_ACTIVE_CFG_RSP, pl, 1, out, sizeof out);
    }
    if (n) q_radio(out, n);
}

/* ---- fake ESP32: records what the STM32 sends, acks every chunk ---- */
static int n_hello, n_chunks, n_end; static uint8_t chunk[600], endp[16]; static uint16_t chunk_len;
static void esp_cb(const hci_msg_t *m, void *ctx)
{
    (void)ctx; uint8_t out[64]; size_t n;
    if (m->msg == LINK_UP_HELLO) n_hello++;
    if (m->msg == LINK_UP_RECORDS) {
        n_chunks++; memcpy(chunk, m->payload, m->len); chunk_len = m->len;
        n = hci_encode(LINK_SAP, LINK_DN_ACK, m->payload, 2, out, sizeof out); q_link(out, n);
    }
    if (m->msg == LINK_UP_END) {
        n_end++; memcpy(endp, m->payload, m->len < 16 ? m->len : 16);
        n = hci_encode(LINK_SAP, LINK_DN_DONE, NULL, 0, out, sizeof out); q_link(out, n);
    }
}

/* ---- port mock ---- */
uint32_t port_millis(void) { return t_ms; }
uint32_t port_utc(void) { return t_utc + t_ms / 1000u; }
void port_set_utc(uint32_t utc) { t_utc = utc; }
void port_rtc_alarm_at(uint32_t utc) { alarm_at = utc; }
void port_enter_stop(void) {}
void port_radio_power(bool on) { radio_on = on; }
void port_radio_send(const uint8_t *d, size_t n) { for (size_t i = 0; i < n; i++) hci_rx_byte(&mod_rx, d[i]); }
bool port_radio_getc(uint8_t *b) { if (rq_t == rq_h) return false; *b = radio_q[rq_t++]; return true; }
void port_esp_power(bool on) {
    if (on && !esp_on) { uint8_t out[16]; size_t n = hci_encode(LINK_SAP, LINK_DN_READY, NULL, 0, out, sizeof out); q_link(out, n); }
    esp_on = on;
}
void port_link_send(const uint8_t *d, size_t n) { for (size_t i = 0; i < n; i++) hci_rx_byte(&esp_rx, d[i]); }
bool port_link_getc(uint8_t *b) { if (lq_t == lq_h) return false; *b = link_q[lq_t++]; return true; }
uint16_t port_battery_mv(void) { return 3600; }
size_t port_flash_load_keys(ks_entry_t *tab, size_t max)
{
    (void)max; memcpy(tab[0].id, &V_M5S[2], 6); memcpy(tab[0].key, V_KEY, 16); return 1;
}
bool port_flash_store_keys(const ks_entry_t *tab, size_t n) { (void)tab; (void)n; return true; }

static void run(uint32_t ms) { for (uint32_t i = 0; i < ms; i += 10) { t_ms += 10; collector_poll(); } }

int main(void)
{
    hci_rx_init(&mod_rx, mod_cb, NULL);
    hci_rx_init(&esp_rx, esp_cb, NULL);
    collector_init();
    CHECK(alarm_at > t_utc && collector_can_sleep());

    collector_on_rtc_alarm();
    run(500);
    CHECK(radio_on && mod_cfg_set && mod_link_mode == IMST_LINK_CT);

    /* two telegrams from the same (known) meter + one from an unknown meter */
    q_radio(V_HCI_RX_UNKNOWN, sizeof V_HCI_RX_UNKNOWN);
    q_radio(V_HCI_RX, sizeof V_HCI_RX);
    q_radio(V_HCI_RX, sizeof V_HCI_RX);
    run(100);
    /* only one known meter -> window closes early, ESP32 session starts */
    run(500);
    CHECK(!radio_on);
    CHECK(n_hello == 1 && n_chunks == 1 && n_end == 1);
    /* chunk = seq(2) | utc(4) rssi(1) status(1) | packet(L+1) */
    CHECK(chunk_len == 2 + 6 + sizeof V_M5S);
    CHECK(chunk[2 + 5] == 1 /* OMS_DECRYPTED */ && (int8_t)chunk[2 + 4] == -80);
    CHECK(memcmp(&chunk[2 + 6 + 15], V_M5S_PLAIN, sizeof V_M5S_PLAIN) == 0);
    /* END = n_rec | n_seen | n_keys | n_unknown | n_bad */
    CHECK(endp[0] == 1 && endp[2] == 1 && endp[4] == 1 && endp[6] == 1 && endp[8] == 0);
    CHECK(!esp_on && collector_can_sleep());
    CHECK(alarm_at % 86400u == 2u * 3600u);                       /* next window 02:00 UTC */

    printf(fails ? "\n%d FAILED\n" : "\nALL PASSED\n", fails);
    return fails != 0;
}
