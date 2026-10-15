#include "hci.h"

#define SLIP_END     0xC0u
#define SLIP_ESC     0xDBu
#define SLIP_ESC_END 0xDCu
#define SLIP_ESC_ESC 0xDDu

uint16_t hci_crc16(const uint8_t *d, size_t n)
{
    uint16_t crc = 0xFFFFu;
    while (n--) {
        crc ^= *d++;
        for (int i = 0; i < 8; i++)
            crc = (crc & 1u) ? (uint16_t)((crc >> 1) ^ 0x8408u) : (uint16_t)(crc >> 1);
    }
    return (uint16_t)~crc;
}

void hci_rx_init(hci_rx_t *rx, hci_rx_cb_t cb, void *ctx)
{
    rx->n = 0;
    rx->esc = false;
    rx->overflow = false;
    rx->cb = cb;
    rx->ctx = ctx;
    rx->crc_errors = 0;
    rx->overflows = 0;
}

static void hci_rx_frame_done(hci_rx_t *rx)
{
    /* minimum: SAP + MSG + FCS(2) */
    if (!rx->overflow && rx->n >= 4u) {
        uint16_t body = (uint16_t)(rx->n - 2u);
        uint16_t fcs  = (uint16_t)(rx->buf[body] | (rx->buf[body + 1u] << 8));
        if (hci_crc16(rx->buf, body) == fcs) {
            hci_msg_t m = { rx->buf[0], rx->buf[1], &rx->buf[2], (uint16_t)(body - 2u) };
            if (rx->cb) rx->cb(&m, rx->ctx);
        } else {
            rx->crc_errors++;
        }
    }
    rx->n = 0;
    rx->esc = false;
    rx->overflow = false;
}

void hci_rx_byte(hci_rx_t *rx, uint8_t b)
{
    if (b == SLIP_END) {            /* frame delimiter (leading or trailing) */
        hci_rx_frame_done(rx);
        return;
    }
    if (b == SLIP_ESC) {
        rx->esc = true;
        return;
    }
    if (rx->esc) {
        rx->esc = false;
        if (b == SLIP_ESC_END)      b = SLIP_END;
        else if (b == SLIP_ESC_ESC) b = SLIP_ESC;
        /* else: protocol violation, keep byte; the CRC will reject the frame */
    }
    if (rx->n < sizeof rx->buf) {
        rx->buf[rx->n++] = b;
    } else if (!rx->overflow) {
        rx->overflow = true;
        rx->overflows++;
    }
}

static bool slip_put(uint8_t *out, size_t *i, size_t max, uint8_t b)
{
    if (b == SLIP_END || b == SLIP_ESC) {
        if (*i + 2u > max) return false;
        out[(*i)++] = SLIP_ESC;
        out[(*i)++] = (b == SLIP_END) ? SLIP_ESC_END : SLIP_ESC_ESC;
    } else {
        if (*i + 1u > max) return false;
        out[(*i)++] = b;
    }
    return true;
}

size_t hci_encode(uint8_t sap, uint8_t msg, const uint8_t *payload,
                  uint16_t len, uint8_t *out, size_t out_max)
{
    if (len > HCI_MAX_PAYLOAD || out_max < 2u) return 0;

    /* CRC over SAP | MSG | payload (unescaped) */
    uint16_t crc = 0xFFFFu;
    uint8_t  hdr[2] = { sap, msg };
    const uint8_t *parts[2] = { hdr, payload };
    size_t lens[2] = { 2u, len };
    for (int p = 0; p < 2; p++) {
        for (size_t k = 0; k < lens[p]; k++) {
            crc ^= parts[p][k];
            for (int i = 0; i < 8; i++)
                crc = (crc & 1u) ? (uint16_t)((crc >> 1) ^ 0x8408u) : (uint16_t)(crc >> 1);
        }
    }
    crc = (uint16_t)~crc;

    size_t i = 0;
    out[i++] = SLIP_END;
    if (!slip_put(out, &i, out_max, sap)) return 0;
    if (!slip_put(out, &i, out_max, msg)) return 0;
    for (uint16_t k = 0; k < len; k++)
        if (!slip_put(out, &i, out_max, payload[k])) return 0;
    if (!slip_put(out, &i, out_max, (uint8_t)(crc & 0xFFu))) return 0;   /* LSB first */
    if (!slip_put(out, &i, out_max, (uint8_t)(crc >> 8))) return 0;
    if (i + 1u > out_max) return 0;
    out[i++] = SLIP_END;
    return i;
}
