#include "keystore.h"

#include <string.h>

#include "port.h"

static ks_entry_t s_tab[KS_MAX_METERS];
static size_t     s_n;

static int find(const uint8_t id[6], bool *found)
{
    int lo = 0, hi = (int)s_n - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        int c = memcmp(s_tab[mid].id, id, 6);
        if (c == 0) { *found = true; return mid; }
        if (c < 0) lo = mid + 1; else hi = mid - 1;
    }
    *found = false;
    return lo;                                   /* insertion point */
}

void ks_init(void)
{
    s_n = port_flash_load_keys(s_tab, KS_MAX_METERS);
}

size_t ks_count(void) { return s_n; }

int ks_index_of(const uint8_t id[6])
{
    bool f;
    int i = find(id, &f);
    return f ? i : -1;
}

const ks_entry_t *ks_at(size_t idx) { return idx < s_n ? &s_tab[idx] : NULL; }

bool ks_put(const uint8_t id[6], const uint8_t key[16])
{
    bool f;
    int i = find(id, &f);
    if (!f) {
        if (s_n >= KS_MAX_METERS) return false;
        memmove(&s_tab[i + 1], &s_tab[i], (s_n - (size_t)i) * sizeof s_tab[0]);
        memcpy(s_tab[i].id, id, 6);
        s_n++;
    }
    memcpy(s_tab[i].key, key, 16);
    return true;
}

bool ks_del(const uint8_t id[6])
{
    bool f;
    int i = find(id, &f);
    if (!f) return false;
    memmove(&s_tab[i], &s_tab[i + 1], (s_n - (size_t)i - 1u) * sizeof s_tab[0]);
    s_n--;
    memset(&s_tab[s_n], 0, sizeof s_tab[0]);
    return true;
}

bool ks_commit(void) { return port_flash_store_keys(s_tab, s_n); }
