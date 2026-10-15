#include "imst_wmbus.h"

bool imst_parse_rx(const hci_msg_t *m, imst_rx_t *out)
{
    /* payload: UTC(4, LSB first) | DecStatus | EncMode | PacketInfo | RSSI | Packet */
    if (m->sap != IMST_SAP_WMBUS || m->msg != IMST_WM_RX_PACKET_IND) return false;
    if (m->len < 8u + 11u) return false;   /* 8 header octets + DLL (10) + CI (1) */

    const uint8_t *p = m->payload;
    out->module_utc = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                      ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    out->dec_status = p[4];
    out->enc_mode   = p[5];
    out->pkt_info   = p[6];
    out->rssi_dbm   = (int8_t)p[7];
    out->pkt        = &p[8];
    out->pkt_len    = (uint16_t)(m->len - 8u);

    /* Trust the HCI length, but sanity-check the L-field (L = bytes after L). */
    if ((uint16_t)out->pkt[0] + 1u > out->pkt_len) return false;
    out->pkt_len = (uint16_t)(out->pkt[0] + 1u);
    return true;
}

size_t imst_build_ping(uint8_t *out, size_t max)
{
    return hci_encode(IMST_SAP_DEVMGMT, IMST_DM_PING_REQ, NULL, 0, out, max);
}

size_t imst_build_get_active_cfg(uint8_t *out, size_t max)
{
    return hci_encode(IMST_SAP_WMBUS, IMST_WM_GET_ACTIVE_CFG_REQ, NULL, 0, out, max);
}

size_t imst_build_set_active_cfg(const uint8_t cfg[IMST_CFG_LEN], uint8_t *out, size_t max)
{
    return hci_encode(IMST_SAP_WMBUS, IMST_WM_SET_ACTIVE_CFG_REQ, cfg, IMST_CFG_LEN, out, max);
}

void imst_cfg_patch(uint8_t cfg[IMST_CFG_LEN], uint8_t link_mode)
{
    uint16_t opt = (uint16_t)(cfg[IMST_CFG_OFS_OPTIONS] | (cfg[IMST_CFG_OFS_OPTIONS + 1u] << 8));
    opt |= IMST_OPT_RX_NOTIFY;
    opt &= (uint16_t)~IMST_OPT_ADDR_FILTER;
    cfg[IMST_CFG_OFS_LINK_MODE]    = link_mode;
    cfg[IMST_CFG_OFS_OPTIONS]      = (uint8_t)(opt & 0xFFu);
    cfg[IMST_CFG_OFS_OPTIONS + 1u] = (uint8_t)(opt >> 8);
    /* UI options: switch the LED signalling off to save battery (p.26) */
    cfg[IMST_CFG_OFS_UI_OPTIONS]      = 0;
    cfg[IMST_CFG_OFS_UI_OPTIONS + 1u] = 0;
}
