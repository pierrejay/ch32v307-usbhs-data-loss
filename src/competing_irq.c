#include "ch32v30x.h"
#include "competing_irq.h"

#define WORK_TIMER_HZ           24000000u
#define WORK_TIMER_TICKS_PER_US (WORK_TIMER_HZ / 1000000u)

#if (WORK_TIMER_HZ % 1000000u) != 0
#error WORK_TIMER_HZ must be an integer multiple of 1 MHz
#endif

#if IRQ_WORK_US > (0xFFFFu / WORK_TIMER_TICKS_PER_US)
#error IRQ_WORK_US does not fit in the TIM3 elapsed-time counter
#endif

static volatile uint16_t Work_Ticks;

void TIM2_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));

void TIM2_IRQHandler(void)
{
    const uint16_t work_ticks = Work_Ticks;
    const uint16_t start = TIM3->CNT;

    TIM_ClearITPendingBit(TIM2, TIM_IT_Update);

    /* TIM3 is a dedicated free-running clock. Unsigned subtraction makes the
     * elapsed-time test independent of its starting value and wraparound. */
    while ((uint16_t)(TIM3->CNT - start) < work_ticks)
        ;
}

void CompetingIRQ_Init(void)
{
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure = { 0 };
    NVIC_InitTypeDef NVIC_InitStructure = { 0 };

    Work_Ticks = (uint16_t)(IRQ_WORK_US * WORK_TIMER_TICKS_PER_US);

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2 | RCC_APB1Periph_TIM3, ENABLE);

    /* TIM3 is the private 24 MHz elapsed-time source. It has no interrupt and
     * is never written again after initialization. */
    TIM_TimeBaseStructure.TIM_Prescaler =
        (uint16_t)(SystemCoreClock / WORK_TIMER_HZ) - 1;
    TIM_TimeBaseStructure.TIM_Period = 0xFFFF;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM3, &TIM_TimeBaseStructure);
    TIM_SetCounter(TIM3, 0);
    TIM_Cmd(TIM3, ENABLE);

    /* TIM2 only generates the competing interrupt. */
    TIM_TimeBaseStructure.TIM_Prescaler = (uint16_t)(SystemCoreClock / 1000000u) - 1;
    TIM_TimeBaseStructure.TIM_Period = COMPETING_IRQ_PERIOD_US - 1;
    TIM_TimeBaseStructure.TIM_ClockDivision = TIM_CKD_DIV1;
    TIM_TimeBaseStructure.TIM_CounterMode = TIM_CounterMode_Up;
    TIM_TimeBaseInit(TIM2, &TIM_TimeBaseStructure);
    TIM_ITConfig(TIM2, TIM_IT_Update, ENABLE);
    TIM_ClearITPendingBit(TIM2, TIM_IT_Update);

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
    NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = COMPETING_IRQ_PREEMPT;
    NVIC_InitStructure.NVIC_IRQChannelSubPriority = COMPETING_IRQ_SUB;
    NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;

    NVIC_InitStructure.NVIC_IRQChannel = USBHS_IRQn;
    NVIC_Init(&NVIC_InitStructure);
    NVIC_InitStructure.NVIC_IRQChannel = TIM2_IRQn;
    NVIC_Init(&NVIC_InitStructure);

    TIM_Cmd(TIM2, ENABLE);
}
