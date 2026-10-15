/*
 * port.h - what the board / STM32 HAL layer must provide to the collector.
 * port_stm32_hal.c implements it on top of the STM32 HAL.
 */
#ifndef PORT_H
#define PORT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "keystore.h"

uint32_t port_millis(void);
uint32_t port_utc(void);                       /* STM32 RTC, seconds since 1970 */
void     port_set_utc(uint32_t utc);
void     port_rtc_alarm_at(uint32_t utc);      /* wake-up from STOP mode */
void     port_enter_stop(void);                /* returns after any wake-up IRQ */

/* iM891A-XL: load switch on VDD (pin 29), USART to TxD (19) / RxD (18) */
void     port_radio_power(bool on);
void     port_radio_send(const uint8_t *d, size_t n);
bool     port_radio_getc(uint8_t *b);          /* pops from the UART RX ring */

/* ESP32: load switch on 3V3, USART link */
void     port_esp_power(bool on);
void     port_link_send(const uint8_t *d, size_t n);
bool     port_link_getc(uint8_t *b);

uint16_t port_battery_mv(void);

size_t   port_flash_load_keys(ks_entry_t *tab, size_t max);
bool     port_flash_store_keys(const ks_entry_t *tab, size_t n);

#endif
