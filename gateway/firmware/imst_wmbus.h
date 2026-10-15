/*
 * imst_wmbus.h - the subset of the IMST WM-Bus Gateway HCI used by the collector
 *
 * Source: IMST "WM-Bus Gateway HCI Protocol Specification" V2.4 (wip, 2026-06-03)
 * Page numbers refer to that document. V2.4 is still marked "wip" by IMST;
 * IDs and layouts are checked against it.
 */
#ifndef IMST_WMBUS_H
#define IMST_WMBUS_H

#include "hci.h"

/* Service Access Points (p.7) */
#define IMST_SAP_DEVMGMT              0x01u
#define IMST_SAP_WMBUS                0x09u

/* Device Management (SAP 01h) */
#define IMST_DM_STARTUP_IND           0x00u   /* event, only if System Option bit 4 set (p.14, 17) */
#define IMST_DM_PING_REQ              0x01u   /* p.9 */
#define IMST_DM_PING_RSP              0x02u   /* payload: Status (p.10) */

/* WM-Bus Gateway (SAP 09h) */
#define IMST_WM_GET_ACTIVE_CFG_REQ    0x01u   /* p.23-24 */
#define IMST_WM_GET_ACTIVE_CFG_RSP    0x02u   /* payload: Status | Gateway Configuration */
#define IMST_WM_SET_ACTIVE_CFG_REQ    0x03u   /* payload: Gateway Configuration, RAM only */
#define IMST_WM_SET_ACTIVE_CFG_RSP    0x04u   /* payload: Status */
#define IMST_WM_RX_PACKET_IND         0x20u   /* event (p.34-36) */

/* Gateway Configuration (p.26): 11 octets */
#define IMST_CFG_LEN                  11u
#define IMST_CFG_OFS_LINK_MODE        0u      /* 1 octet */
#define IMST_CFG_OFS_OPTIONS          1u      /* 2 octets, LSB first */
#define IMST_CFG_OFS_UI_OPTIONS       3u      /* 2 octets, LSB first */
#define IMST_CFG_OFS_LED_TIMING       5u      /* 2 octets, ms */
#define IMST_CFG_OFS_RECAL_TIMEOUT    7u      /* 4 octets, ms, >= 100 */

/* Link Mode values (p.26) */
#define IMST_LINK_OFF                 0u
#define IMST_LINK_S                   1u
#define IMST_LINK_T                   2u
#define IMST_LINK_CT                  3u
#define IMST_LINK_C                   5u

/* Options bits (p.26) */
#define IMST_OPT_ADDR_FILTER          (1u << 0)  /* only Device List (max 8 items!) */
#define IMST_OPT_RX_NOTIFY            (1u << 1)
#define IMST_OPT_TX_NOTIFY            (1u << 2)
#define IMST_OPT_RECALIBRATION        (1u << 3)

/* Status codes (p.54, WM-Bus SAP) */
#define IMST_STATUS_OK                0x00u

/* Decryption Status in the Rx event (p.35) */
#define IMST_DEC_NOT_ENCRYPTED        0u
#define IMST_DEC_OK                   1u
#define IMST_DEC_NO_KEY               2u
#define IMST_DEC_MODE_UNSUPPORTED     3u
#define IMST_DEC_ERROR                4u

typedef struct {
    uint32_t       module_utc;  /* module clock; reset on power-up -> not used */
    uint8_t        dec_status;  /* IMST_DEC_* */
    uint8_t        enc_mode;    /* 0, 5, 7, 128, 129, 255 */
    uint8_t        pkt_info;    /* 1=S, 2=T, 5=C 100k A, 21=C 100k B ... (p.35) */
    int8_t         rssi_dbm;
    const uint8_t *pkt;         /* L C M M A A A A V T CI ... (CRCs removed, p.36) */
    uint16_t       pkt_len;
} imst_rx_t;

/* Parse SAP 09h / Msg 20h. Returns false if m is not a valid Rx event. */
bool imst_parse_rx(const hci_msg_t *m, imst_rx_t *out);

/* Frame builders: return encoded length (0 on error). */
size_t imst_build_ping(uint8_t *out, size_t max);
size_t imst_build_get_active_cfg(uint8_t *out, size_t max);
size_t imst_build_set_active_cfg(const uint8_t cfg[IMST_CFG_LEN], uint8_t *out, size_t max);

/* Take the configuration read from the module and change only what the
 * collector needs: link mode, Rx notification on, address filter off
 * (decryption is done on the STM32 with its own key store). */
void imst_cfg_patch(uint8_t cfg[IMST_CFG_LEN], uint8_t link_mode);

#endif
