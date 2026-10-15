/*
 * port_stm32_hal.c - STM32 HAL glue (family independent where possible)
 *
 * CubeMX handles used here:
 *   huart1 : iM891A-XL  - 115200 8N1, PA9/PA10 style TX/RX, 3.3 V
 *   huart2 : ESP32 link - 115200 8N1 (or faster), 3.3 V
 *   hrtc   : LSE 32.768 kHz, binary/BCD calendar, Alarm A with interrupt
 *   hadc1  : battery divider
 *   GPIO   : RADIO_EN (load switch -> iM891A-XL VDD), ESP_EN (load switch -> ESP32 3V3)
 *
 * Family-specific parts are marked TODO (flash programming granularity,
 * STOP mode variant, clock re-init after STOP).
 */
#include "main.h"            /* CubeMX: HAL handles, pin labels */
#include "port.h"
#include "collector.h"

#include <string.h>

extern UART_HandleTypeDef huart1, huart2;
extern RTC_HandleTypeDef  hrtc;
extern ADC_HandleTypeDef  hadc1;
void SystemClock_Config(void);

/* ------------------------------------------------ UART RX ring buffers */
typedef struct { volatile uint16_t head, tail; uint8_t buf[1024]; uint8_t byte; } ring_t;
static ring_t s_radio, s_link;

static void ring_push(ring_t *r, uint8_t b)
{
    uint16_t n = (uint16_t)((r->head + 1u) % sizeof r->buf);
    if (n != r->tail) { r->buf[r->head] = b; r->head = n; }  /* else: drop */
}
static bool ring_pop(ring_t *r, uint8_t *b)
{
    if (r->tail == r->head) return false;
    *b = r->buf[r->tail];
    r->tail = (uint16_t)((r->tail + 1u) % sizeof r->buf);
    return true;
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *h)
{
    if (h == &huart1) { ring_push(&s_radio, s_radio.byte); HAL_UART_Receive_IT(h, &s_radio.byte, 1); }
    if (h == &huart2) { ring_push(&s_link,  s_link.byte);  HAL_UART_Receive_IT(h, &s_link.byte, 1); }
}

bool port_radio_getc(uint8_t *b) { return ring_pop(&s_radio, b); }
bool port_link_getc(uint8_t *b)  { return ring_pop(&s_link, b); }

void port_radio_send(const uint8_t *d, size_t n) { HAL_UART_Transmit(&huart1, (uint8_t *)d, (uint16_t)n, 100); }
void port_link_send(const uint8_t *d, size_t n)  { HAL_UART_Transmit(&huart2, (uint8_t *)d, (uint16_t)n, 200); }

/* ------------------------------------------------ power switches */
void port_radio_power(bool on)
{
    if (on) {
        HAL_GPIO_WritePin(RADIO_EN_GPIO_Port, RADIO_EN_Pin, GPIO_PIN_SET);
        s_radio.head = s_radio.tail = 0;
        HAL_UART_Receive_IT(&huart1, &s_radio.byte, 1);
    } else {
        HAL_UART_AbortReceive_IT(&huart1);
        HAL_GPIO_WritePin(RADIO_EN_GPIO_Port, RADIO_EN_Pin, GPIO_PIN_RESET);
        /* TODO: set USART1 TX pin to analog/low while the module is unpowered
         * to avoid back-powering it through its RxD pin. */
    }
}

void port_esp_power(bool on)
{
    if (on) {
        HAL_GPIO_WritePin(ESP_EN_GPIO_Port, ESP_EN_Pin, GPIO_PIN_SET);
        s_link.head = s_link.tail = 0;
        HAL_UART_Receive_IT(&huart2, &s_link.byte, 1);
    } else {
        HAL_UART_AbortReceive_IT(&huart2);
        HAL_GPIO_WritePin(ESP_EN_GPIO_Port, ESP_EN_Pin, GPIO_PIN_RESET);
    }
}

/* ------------------------------------------------ time (RTC) */
static uint32_t days_from_civil(int y, unsigned m, unsigned d)
{
    y -= m <= 2;
    int era = (y >= 0 ? y : y - 399) / 400;
    unsigned yoe = (unsigned)(y - era * 400);
    unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (uint32_t)(era * 146097 + (int)doe - 719468);
}

uint32_t port_millis(void) { return HAL_GetTick(); }

uint32_t port_utc(void)
{
    RTC_TimeTypeDef t; RTC_DateTypeDef d;
    HAL_RTC_GetTime(&hrtc, &t, RTC_FORMAT_BIN);
    HAL_RTC_GetDate(&hrtc, &d, RTC_FORMAT_BIN);       /* must follow GetTime */
    return days_from_civil(2000 + d.Year, d.Month, d.Date) * 86400u +
           t.Hours * 3600u + t.Minutes * 60u + t.Seconds;
}

void port_set_utc(uint32_t utc)
{
    uint32_t z = utc / 86400u + 719468u, s = utc % 86400u;
    uint32_t era = z / 146097u, doe = z - era * 146097u;
    uint32_t yoe = (doe - doe / 1460u + doe / 36524u - doe / 146096u) / 365u;
    uint32_t doy = doe - (365u * yoe + yoe / 4u - yoe / 100u);
    uint32_t mp = (5u * doy + 2u) / 153u;
    uint32_t d = doy - (153u * mp + 2u) / 5u + 1u;
    uint32_t m = mp < 10u ? mp + 3u : mp - 9u;
    uint32_t y = yoe + era * 400u + (m <= 2u);

    RTC_TimeTypeDef t = {0}; RTC_DateTypeDef dt = {0};
    t.Hours = (uint8_t)(s / 3600u); t.Minutes = (uint8_t)(s / 60u % 60u); t.Seconds = (uint8_t)(s % 60u);
    dt.Year = (uint8_t)(y - 2000u); dt.Month = (uint8_t)m; dt.Date = (uint8_t)d; dt.WeekDay = RTC_WEEKDAY_MONDAY;
    HAL_RTC_SetTime(&hrtc, &t, RTC_FORMAT_BIN);
    HAL_RTC_SetDate(&hrtc, &dt, RTC_FORMAT_BIN);
}

void port_rtc_alarm_at(uint32_t utc)
{
    uint32_t s = utc % 86400u, z = utc / 86400u;
    (void)z;
    RTC_AlarmTypeDef a = {0};
    a.AlarmTime.Hours = (uint8_t)(s / 3600u);
    a.AlarmTime.Minutes = (uint8_t)(s / 60u % 60u);
    a.AlarmTime.Seconds = (uint8_t)(s % 60u);
    a.AlarmMask = RTC_ALARMMASK_DATEWEEKDAY;           /* daily match on h:m:s */
    a.Alarm = RTC_ALARM_A;
    /* TODO: for period_s != 86400 use the RTC wake-up timer or a date match. */
    HAL_RTC_SetAlarm_IT(&hrtc, &a, RTC_FORMAT_BIN);
}

void HAL_RTC_AlarmAEventCallback(RTC_HandleTypeDef *h) { (void)h; collector_on_rtc_alarm(); }

void port_enter_stop(void)
{
    HAL_SuspendTick();
    HAL_PWR_EnterSTOPMode(PWR_LOWPOWERREGULATOR_ON, PWR_STOPENTRY_WFI);  /* TODO: STOP2 on L4/U5 */
    SystemClock_Config();                              /* clocks are reset after STOP */
    HAL_ResumeTick();
}

/* ------------------------------------------------ battery */
uint16_t port_battery_mv(void)
{
    HAL_ADC_Start(&hadc1);
    HAL_ADC_PollForConversion(&hadc1, 10);
    uint32_t raw = HAL_ADC_GetValue(&hadc1);
    HAL_ADC_Stop(&hadc1);
    return (uint16_t)(raw * 3300u * 2u / 4095u);       /* TODO: divider ratio, VREFINT */
}

/* ------------------------------------------------ key store in flash */
size_t port_flash_load_keys(ks_entry_t *tab, size_t max)
{
    /* TODO: read a reserved flash page: magic | count | entries | CRC32 */
    (void)tab; (void)max;
    return 0;
}

bool port_flash_store_keys(const ks_entry_t *tab, size_t n)
{
    /* TODO: erase page + HAL_FLASH_Program (32/64/128-bit granularity per family),
     * ideally two pages A/B so a power loss never leaves an empty key store. */
    (void)tab; (void)n;
    return true;
}

/* ------------------------------------------------ main loop */
void app_main(void)
{
    collector_init();
    for (;;) {
        collector_poll();
        if (collector_can_sleep()) port_enter_stop();  /* RTC alarm wakes us */
        else __WFI();                                  /* sleep until next UART IRQ / tick */
    }
}
