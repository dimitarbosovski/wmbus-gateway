/* Host unit tests (Linux, gcc, mbedTLS). Build: make -C test */
#include <stdio.h>
#include <string.h>

#include "../firmware/hci.h"
#include "../firmware/imst_wmbus.h"
#include "../firmware/oms.h"
#include "vectors.h"

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); fails++; } \
                      else printf("ok   %s\n", #c); } while (0)

/* ---- HCI ---- */
static hci_msg_t last; static uint8_t last_buf[600]; static int got;
static void cb(const hci_msg_t *m, void *ctx)
{
    (void)ctx; last = *m; memcpy(last_buf, m->payload, m->len); last.payload = last_buf; got++;
}

static void test_hci(void)
{
    const uint8_t chk[] = "123456789";
    CHECK(hci_crc16(chk, 9) == 0x906E);                       /* CRC-16/IBM-SDLC check */

    uint8_t out[32];
    size_t n = imst_build_ping(out, sizeof out);               /* spec p.9 example */
    const uint8_t ping[] = { 0xC0, 0x01, 0x01, 0x16, 0x07, 0xC0 };
    CHECK(n == sizeof ping && memcmp(out, ping, n) == 0);

    hci_rx_t rx; hci_rx_init(&rx, cb, NULL);
    const uint8_t pong[] = { 0xC0, 0x01, 0x02, 0x00, 0xA0, 0xAF, 0xC0 };  /* spec p.9 */
    got = 0;
    for (size_t i = 0; i < sizeof pong; i++) hci_rx_byte(&rx, pong[i]);
    CHECK(got == 1 && last.sap == 0x01 && last.msg == 0x02 && last.len == 1 && last.payload[0] == 0);

    /* SLIP escaping round trip (payload containing C0 and DB) */
    const uint8_t pl[] = { 0xC0, 0xDB, 0x00, 0xC0 };
    uint8_t enc[64];
    n = hci_encode(0x80, 0x02, pl, sizeof pl, enc, sizeof enc);
    got = 0;
    for (size_t i = 0; i < n; i++) hci_rx_byte(&rx, enc[i]);
    CHECK(got == 1 && last.len == sizeof pl && memcmp(last.payload, pl, sizeof pl) == 0);

    /* corrupted frame is rejected */
    enc[3] ^= 0x01; got = 0;
    for (size_t i = 0; i < n; i++) hci_rx_byte(&rx, enc[i]);
    CHECK(got == 0 && rx.crc_errors == 1);

    /* Rx event built by the Python generator */
    got = 0;
    for (size_t i = 0; i < sizeof V_HCI_RX; i++) hci_rx_byte(&rx, V_HCI_RX[i]);
    imst_rx_t ev;
    CHECK(got == 1 && imst_parse_rx(&last, &ev));
    CHECK(ev.dec_status == IMST_DEC_NO_KEY && ev.enc_mode == 5 && ev.rssi_dbm == -80);
    CHECK(ev.pkt_len == sizeof V_M5S && memcmp(ev.pkt, V_M5S, sizeof V_M5S) == 0);
}

/* ---- OMS ---- */
static void test_oms(void)
{
    /* OMS Annex N.2.3: Mk = 00..0F, MCR = B3 0A 00 00, ID = 78 56 34 12 */
    const uint8_t mcr[4] = { 0xB3, 0x0A, 0x00, 0x00 }, id[4] = { 0x78, 0x56, 0x34, 0x12 };
    const uint8_t kenc_ref[16] = { 0xEC,0xCF,0x39,0xD4,0x75,0xD7,0x30,0xB8,0x28,0x4F,0xDF,0xDC,0x19,0x95,0xD5,0x2F };
    const uint8_t kmac_ref[16] = { 0xC9,0xCD,0x19,0xFF,0x5A,0x9A,0xAD,0x5A,0x6B,0xBD,0xA1,0x3B,0xD2,0xC4,0xC7,0xAD };
    uint8_t k[16];
    CHECK(oms_kdf_mode7(V_KEY, 0x00, mcr, id, k) == 0 && memcmp(k, kenc_ref, 16) == 0);
    CHECK(oms_kdf_mode7(V_KEY, 0x01, mcr, id, k) == 0 && memcmp(k, kmac_ref, 16) == 0);
    CHECK(memcmp(V_M7_KENC_PY, kenc_ref, 16) == 0);           /* python agrees with Annex N */

    uint8_t b[256];
    uint8_t wrong[16]; memcpy(wrong, V_KEY, 16); wrong[15] ^= 0xFF;

    memcpy(b, V_M5S, sizeof V_M5S);
    CHECK(oms_decrypt_inplace(b, sizeof V_M5S, V_KEY) == OMS_DECRYPTED);
    CHECK(memcmp(&b[15], V_M5S_PLAIN, sizeof V_M5S_PLAIN) == 0);   /* 10 DLL + 5 TPL */

    memcpy(b, V_M5S, sizeof V_M5S);
    CHECK(oms_decrypt_inplace(b, sizeof V_M5S, wrong) == OMS_CHECK_FAIL);

    memcpy(b, V_M5S, sizeof V_M5S);
    CHECK(oms_decrypt_inplace(b, sizeof V_M5S, NULL) == OMS_NO_KEY);

    memcpy(b, V_M5L, sizeof V_M5L);
    CHECK(oms_decrypt_inplace(b, sizeof V_M5L, V_KEY) == OMS_DECRYPTED);

    memcpy(b, V_M7, sizeof V_M7);
    CHECK(oms_decrypt_inplace(b, sizeof V_M7, V_KEY) == OMS_DECRYPTED);
    /* 10 DLL + 17 AFL + 6 TPL (CI ACC STS CFG CFG-ext) */
    CHECK(memcmp(&b[33], V_M7_PLAIN, sizeof V_M7_PLAIN) == 0);

    memcpy(b, V_M7_BADMAC, sizeof V_M7_BADMAC);
    CHECK(oms_decrypt_inplace(b, sizeof V_M7_BADMAC, V_KEY) == OMS_MAC_FAIL);

    memcpy(b, V_M7, sizeof V_M7);
    CHECK(oms_decrypt_inplace(b, sizeof V_M7, wrong) == OMS_MAC_FAIL);
}

int main(void)
{
    test_hci();
    test_oms();
    printf(fails ? "\n%d FAILED\n" : "\nALL PASSED\n", fails);
    return fails != 0;
}
