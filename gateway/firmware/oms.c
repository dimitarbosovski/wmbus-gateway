#include "oms.h"

#include <string.h>

#include "mbedtls/aes.h"
#include "mbedtls/cipher.h"
#include "mbedtls/cmac.h"
/* mbedTLS config needs: MBEDTLS_AES_C, MBEDTLS_CIPHER_MODE_CBC,
 * MBEDTLS_CIPHER_C, MBEDTLS_CMAC_C */

#define CI_AFL        0x90u
#define CI_TPL_SHORT  0x7Au
#define CI_TPL_LONG   0x72u

/* AFL Fragmentation Control bits (EN 13757-7; cross-checked with wmbusmeters) */
#define AFL_FC_KEYINFO   0x0200u
#define AFL_FC_MAC       0x0400u
#define AFL_FC_COUNTER   0x0800u
#define AFL_FC_LEN       0x1000u
#define AFL_FC_CONTROL   0x2000u
#define AFL_FC_MORE      0x4000u

static void wipe(void *p, size_t n)
{
    volatile uint8_t *v = (volatile uint8_t *)p;
    while (n--) *v++ = 0;
}

static int aes128_cbc_decrypt(const uint8_t key[16], uint8_t iv[16], uint8_t *buf, size_t n)
{
    uint8_t tmp[256];
    if (n > sizeof tmp || (n % 16u) != 0u) return -1;
    memcpy(tmp, buf, n);
    mbedtls_aes_context c;
    mbedtls_aes_init(&c);
    int r = mbedtls_aes_setkey_dec(&c, key, 128);
    if (r == 0) r = mbedtls_aes_crypt_cbc(&c, MBEDTLS_AES_DECRYPT, n, iv, tmp, buf);
    mbedtls_aes_free(&c);
    wipe(tmp, sizeof tmp);
    return r;
}

static int aes128_cmac(const uint8_t key[16], const uint8_t *in, size_t n, uint8_t out[16])
{
    const mbedtls_cipher_info_t *ci = mbedtls_cipher_info_from_type(MBEDTLS_CIPHER_AES_128_ECB);
    if (ci == NULL) return -1;
    return mbedtls_cipher_cmac(ci, key, 128 /* bits */, in, n, out);
}

int oms_kdf_mode7(const uint8_t mk[16], uint8_t dc, const uint8_t mcr[4],
                  const uint8_t id[4], uint8_t out[16])
{
    /* OMS Annex N.3.1: K = CMAC(Mk, DC || MCR || IdentNo || 07 x 7)
     * DC = 0x00 -> Kenc (meter -> other), 0x01 -> Kmac. */
    uint8_t in[16];
    in[0] = dc;
    memcpy(&in[1], mcr, 4);
    memcpy(&in[5], id, 4);
    memset(&in[9], 0x07, 7);
    return aes128_cmac(mk, in, sizeof in, out);
}

void oms_meter_id(const uint8_t *pkt, uint8_t out6[6])
{
    memcpy(out6, &pkt[2], 6);   /* M(2) + ID(4) from the DLL header */
}

static const uint8_t afl_mac_len[16] = { 0, 0, 0, 2, 4, 8, 12, 16, 12, 0, 0, 0, 0, 0, 0, 0 };

typedef struct {
    int      present;
    uint8_t  mcl;
    uint8_t  mcr[4];
    int      has_mcr;
    const uint8_t *mac;
    uint8_t  mac_len;
    const uint8_t *ml;          /* 2 bytes or NULL */
} afl_t;

oms_result_t oms_decrypt_inplace(uint8_t *pkt, size_t pkt_len, const uint8_t key[16])
{
    if (pkt_len < 11u || (size_t)pkt[0] + 1u != pkt_len) return OMS_FORMAT_ERR;

    size_t t = 10;                               /* first CI after DLL */
    afl_t afl = { 0 };

    /* ---- optional AFL (CI 0x90) ---- */
    if (pkt[t] == CI_AFL) {
        if (t + 4u > pkt_len) return OMS_FORMAT_ERR;
        size_t afl_end = t + 2u + pkt[t + 1u];    /* AFL.L counts bytes after AFL.L */
        uint16_t fc = (uint16_t)(pkt[t + 2u] | (pkt[t + 3u] << 8));
        size_t p = t + 4u;
        if (afl_end > pkt_len) return OMS_FORMAT_ERR;
        if (fc & AFL_FC_MORE) return OMS_UNSUPPORTED;      /* fragmented */
        afl.present = 1;
        if (fc & AFL_FC_CONTROL) { afl.mcl = pkt[p]; p += 1u; }
        if (fc & AFL_FC_KEYINFO) { p += 2u; }
        if (fc & AFL_FC_COUNTER) { memcpy(afl.mcr, &pkt[p], 4); afl.has_mcr = 1; p += 4u; }
        if (fc & AFL_FC_MAC) {
            afl.mac_len = afl_mac_len[afl.mcl & 0x0Fu];
            if (afl.mac_len == 0u) return OMS_UNSUPPORTED;
            afl.mac = &pkt[p];
            p += afl.mac_len;
        }
        if (fc & AFL_FC_LEN) { afl.ml = &pkt[p]; p += 2u; }  /* TODO: field order vs EN 13757-7 */
        if (p > afl_end) return OMS_FORMAT_ERR;
        t = afl_end;
        if (t >= pkt_len) return OMS_FORMAT_ERR;
    }

    /* ---- TPL header ---- */
    uint8_t iv[16];
    const uint8_t *m_field, *a_field;            /* a_field = ID(4) Ver Type */
    size_t cfg_pos;
    if (pkt[t] == CI_TPL_SHORT) {
        if (t + 5u > pkt_len) return OMS_FORMAT_ERR;
        m_field = &pkt[2];
        a_field = &pkt[4];
        iv[8]   = pkt[t + 1u];                   /* ACC */
        cfg_pos = t + 3u;
    } else if (pkt[t] == CI_TPL_LONG) {
        if (t + 13u > pkt_len) return OMS_FORMAT_ERR;
        a_field = &pkt[t + 1u];                  /* ID(4) */
        m_field = &pkt[t + 5u];                  /* M(2), then Ver, Type at t+7, t+8 */
        iv[8]   = pkt[t + 9u];                   /* ACC */
        cfg_pos = t + 11u;
    } else {
        return OMS_UNSUPPORTED;                  /* ELL, compact frames, ... */
    }

    uint16_t cfg  = (uint16_t)(pkt[cfg_pos] | (pkt[cfg_pos + 1u] << 8));
    uint8_t  mode = (uint8_t)((cfg >> 8) & 0x1Fu);
    uint8_t  nblk = (uint8_t)((cfg >> 4) & 0x0Fu);
    size_t   d    = cfg_pos + 2u;                /* first encrypted byte */

    if (mode == 0u) return OMS_PLAIN;
    if (key == NULL) return OMS_NO_KEY;

    if (mode == 5u) {
        /* IV = M(2) | ID(4) | Ver | Type | ACC x 8   (OMS Annex N.4.1) */
        uint8_t acc = iv[8];
        iv[0] = m_field[0];
        iv[1] = m_field[1];
        if (pkt[t] == CI_TPL_LONG) {
            memcpy(&iv[2], a_field, 4);          /* ID */
            iv[6] = pkt[t + 7u];                 /* Ver */
            iv[7] = pkt[t + 8u];                 /* Type */
        } else {
            memcpy(&iv[2], a_field, 6);          /* ID Ver Type from DLL */
        }
        memset(&iv[8], acc, 8);

        size_t n = nblk ? (size_t)nblk * 16u : ((pkt_len - d) / 16u) * 16u;
        if (n == 0u || d + n > pkt_len) return OMS_FORMAT_ERR;
        if (aes128_cbc_decrypt(key, iv, &pkt[d], n) != 0) return OMS_FORMAT_ERR;
        return (pkt[d] == 0x2Fu && pkt[d + 1u] == 0x2Fu) ? OMS_DECRYPTED : OMS_CHECK_FAIL;
    }

    if (mode == 7u) {
        if (!afl.present || !afl.has_mcr || afl.mac == NULL) return OMS_FORMAT_ERR;
        if (d + 1u > pkt_len) return OMS_FORMAT_ERR;
        uint8_t cfg_ext = pkt[d];
        d += 1u;
        if (((cfg_ext >> 4) & 0x03u) != 1u) return OMS_UNSUPPORTED;   /* KDF-A only */

        const uint8_t *id = (pkt[t] == CI_TPL_LONG) ? a_field : &pkt[4];
        uint8_t kenc[16], kmac[16], mac[16];
        if (oms_kdf_mode7(key, 0x00u, afl.mcr, id, kenc) != 0 ||
            oms_kdf_mode7(key, 0x01u, afl.mcr, id, kmac) != 0) {
            return OMS_FORMAT_ERR;
        }

        /* AFL.MAC = CMAC(Kmac, MCL || MCR || [ML] || TPL-CI ... last byte) */
        uint8_t macin[1 + 4 + 2 + 256];
        size_t  k = 0;
        macin[k++] = afl.mcl;
        memcpy(&macin[k], afl.mcr, 4); k += 4u;
        if (afl.ml) { macin[k++] = afl.ml[0]; macin[k++] = afl.ml[1]; }
        memcpy(&macin[k], &pkt[t], pkt_len - t); k += pkt_len - t;
        int r = aes128_cmac(kmac, macin, k, mac);
        wipe(kmac, sizeof kmac);
        if (r != 0 || memcmp(mac, afl.mac, afl.mac_len) != 0) {
            wipe(kenc, sizeof kenc);
            return OMS_MAC_FAIL;
        }

        size_t n = nblk ? (size_t)nblk * 16u : ((pkt_len - d) / 16u) * 16u;
        if (n == 0u || d + n > pkt_len) { wipe(kenc, sizeof kenc); return OMS_FORMAT_ERR; }
        memset(iv, 0, sizeof iv);                /* mode 7: static zero IV */
        r = aes128_cbc_decrypt(kenc, iv, &pkt[d], n);
        wipe(kenc, sizeof kenc);
        if (r != 0) return OMS_FORMAT_ERR;
        return (pkt[d] == 0x2Fu && pkt[d + 1u] == 0x2Fu) ? OMS_DECRYPTED : OMS_CHECK_FAIL;
    }

    return OMS_UNSUPPORTED;
}
