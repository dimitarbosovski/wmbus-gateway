/*
 * hci.h - SLIP + CRC16 framing for the IMST WM-Bus Gateway HCI
 *
 * Source: IMST "WM-Bus Gateway HCI Protocol Specification" V2.4 (wip, 2026-06-03)
 *   - UART 115200 8N1                                         (p.5)
 *   - SLIP framing per RFC 1055                               (p.5)
 *   - Frame: SAP ID (1) | Msg ID (1) | Payload (0..500) | FCS (2, LSB first)
 *   - FCS: CRC-16/IBM-SDLC (poly 0x1021 reflected, init 0xFFFF, xorout 0xFFFF,
 *          check 0x906E)                                       (pp.5-6, 9)
 *   - Ping request on the wire: C0 01 01 16 07 C0              (p.9)
 *
 * The same framer is reused for the STM32 <-> ESP32 link (own SAP 0x80).
 */
#ifndef HCI_H
#define HCI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HCI_MAX_PAYLOAD 500u
#define HCI_MAX_FRAME   (2u + HCI_MAX_PAYLOAD + 2u) /* SAP+MSG + payload + FCS */

typedef struct {
    uint8_t        sap;
    uint8_t        msg;
    const uint8_t *payload;
    uint16_t       len;
} hci_msg_t;

typedef void (*hci_rx_cb_t)(const hci_msg_t *m, void *ctx);

typedef struct {
    uint8_t     buf[HCI_MAX_FRAME];
    uint16_t    n;
    bool        esc;
    bool        overflow;
    hci_rx_cb_t cb;
    void       *ctx;
    uint32_t    crc_errors;
    uint32_t    overflows;
} hci_rx_t;

uint16_t hci_crc16(const uint8_t *d, size_t n);

void hci_rx_init(hci_rx_t *rx, hci_rx_cb_t cb, void *ctx);

/* Feed received UART bytes one by one. Call from the main loop (drain an
 * ISR/DMA ring buffer), not from the ISR itself: the callback runs here. */
void hci_rx_byte(hci_rx_t *rx, uint8_t b);

/* Builds C0 | SLIP(SAP MSG payload FCS) | C0 into out.
 * Returns the number of bytes written, 0 if out_max is too small. */
size_t hci_encode(uint8_t sap, uint8_t msg, const uint8_t *payload,
                  uint16_t len, uint8_t *out, size_t out_max);

/* Worst case encoded size (every byte escaped + 2 x END). */
#define HCI_ENCODED_MAX(len) (2u * (2u + (len) + 2u) + 2u)

#endif
