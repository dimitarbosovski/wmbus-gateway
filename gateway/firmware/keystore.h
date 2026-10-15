/*
 * keystore.h - per-meter AES keys on the STM32 (filled from the server via downlink)
 *
 * 250 meters x 22 bytes = 5.5 kB. Sorted array + binary search.
 * Persisted in internal flash through port_flash_*(); enable flash read-out
 * protection (RDP) on shipped units so keys cannot be read over SWD.
 */
#ifndef KEYSTORE_H
#define KEYSTORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define KS_MAX_METERS 256u

typedef struct {
    uint8_t id[6];      /* M(2) + ID(4) as on air (see oms_meter_id) */
    uint8_t key[16];
} ks_entry_t;

void              ks_init(void);                 /* load from flash */
size_t            ks_count(void);
int               ks_index_of(const uint8_t id[6]);   /* -1 if unknown */
const ks_entry_t *ks_at(size_t idx);
bool              ks_put(const uint8_t id[6], const uint8_t key[16]);  /* add/replace */
bool              ks_del(const uint8_t id[6]);
bool              ks_commit(void);               /* write to flash */

#endif
