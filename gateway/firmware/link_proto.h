/*
 * link_proto.h - STM32 <-> ESP32 messages (own protocol, same SLIP+CRC framer
 * as the IMST HCI, SAP 0x80).
 */
#ifndef LINK_PROTO_H
#define LINK_PROTO_H

#define LINK_SAP              0x80u

/* STM32 -> ESP32 */
#define LINK_UP_HELLO         0x01u  /* utc u32 | batt_mv u16 | n_keys u16             */
#define LINK_UP_RECORDS       0x02u  /* seq u16 | record...                           */
#define LINK_UP_END           0x03u  /* n_rec u16 | n_seen u16 | n_keys u16 |
                                        n_unknown u16 | n_bad u16                      */
/* record = utc u32 | rssi i8 | status u8 | wM-Bus packet (L+1 bytes, decrypted
 * in place when status = 1; header unchanged -> DIF/VIF parsing on the server) */

/* ESP32 -> STM32 */
#define LINK_DN_READY         0x10u  /* ESP32 booted, link up                         */
#define LINK_DN_ACK           0x11u  /* seq u16 - chunk published (MQTT QoS1 PUBACK)   */
#define LINK_DN_TIME          0x12u  /* utc u32 (from network time)                   */
#define LINK_DN_CFG           0x13u  /* period_s u32 | offset_s u32 | window_s u16 |
                                        link_mode u8                                   */
#define LINK_DN_KEY_PUT       0x14u  /* n u8 | n x (id[6] | key[16])  (n <= 22)        */
#define LINK_DN_KEY_DEL       0x15u  /* n u8 | n x id[6]                               */
#define LINK_DN_DONE          0x1Fu  /* session finished, STM32 may cut ESP32 power    */

/* EG915N: AT+QMTPUBEX accepts max 1500 bytes per publish (Quectel MQTT AN V1.4).
 * One LINK_UP_RECORDS frame (<= 500 bytes payload) = one MQTT message, QoS 1;
 * the ESP32 answers LINK_DN_ACK(seq) after the broker's PUBACK. */

#endif
