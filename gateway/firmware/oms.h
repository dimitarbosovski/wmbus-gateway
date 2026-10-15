/*
 * oms.h - OMS / EN 13757 TPL decryption on the host MCU (security modes 5 and 7)
 *
 * Input is the wM-Bus packet exactly as the iM891A-XL delivers it in the Rx
 * event: L C M M A A A A V T CI ..., link-layer CRCs already removed.
 *
 * References used:
 *  - OMS Spec Annex N (message examples): mode 5 IV = M + A + 8 x ACC (N.4.1),
 *    mode 7 KDF Kenc/Kmac = CMAC(Mk, DC || MCR || ID || 07 x 7) (N.2.3, N.3.1),
 *    truncated CMAC = first 8 bytes (N.3.2).
 *  - wmbusmeters (GPL-3.0) used only as a cross-check of field layouts
 *    (AFL field order, mode 7 IV = 0, MAC input). No code copied.
 *
 * Not supported yet: ELL (CI 8C/8D, AES-CTR), mode 10 (AES-CCM),
 * fragmented AFL messages, compact frames.
 */
#ifndef OMS_H
#define OMS_H

#include <stddef.h>
#include <stdint.h>

typedef enum {
    OMS_PLAIN       = 0,  /* security mode 0, nothing to do          */
    OMS_DECRYPTED   = 1,  /* decrypted in place, 2F2F check passed   */
    OMS_NO_KEY      = 2,
    OMS_UNSUPPORTED = 3,  /* CI / security mode not handled here     */
    OMS_CHECK_FAIL  = 4,  /* 2F2F mismatch -> wrong key              */
    OMS_FORMAT_ERR  = 5,
    OMS_MAC_FAIL    = 6   /* mode 7 AFL MAC mismatch                 */
} oms_result_t;

/* Meter key-store lookup key: M-field (2, as on air) + ID (4, as on air). */
void oms_meter_id(const uint8_t *pkt, uint8_t out6[6]);

/* Decrypts the TPL payload in place. key may be NULL (-> OMS_NO_KEY if
 * the telegram is encrypted). pkt_len = L + 1. */
oms_result_t oms_decrypt_inplace(uint8_t *pkt, size_t pkt_len, const uint8_t key[16]);

/* Exposed for unit tests. */
int oms_kdf_mode7(const uint8_t mk[16], uint8_t dc, const uint8_t mcr[4],
                  const uint8_t id[4], uint8_t out[16]);

#endif
