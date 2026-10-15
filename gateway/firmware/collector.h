/*
 * collector.h - battery wM-Bus collector: receive window -> decrypt -> upload
 */
#ifndef COLLECTOR_H
#define COLLECTOR_H

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t period_s;   /* time between windows, default 86400 (1 per day)      */
    uint32_t offset_s;   /* window start, seconds after 00:00 UTC                */
    uint16_t window_s;   /* max listen time; meters send every <= 60 s -> 180 s  */
    uint8_t  link_mode;  /* IMST_LINK_CT by default (C1 + T1 on 868.95 MHz)      */
} collector_cfg_t;

void collector_init(void);
void collector_poll(void);          /* call from the main loop */
void collector_on_rtc_alarm(void);  /* call from the RTC alarm IRQ */
bool collector_can_sleep(void);     /* true -> main loop may enter STOP mode */

#endif
