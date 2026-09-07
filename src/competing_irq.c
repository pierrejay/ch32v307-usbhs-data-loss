#include "ch32v30x.h"
#include "competing_irq.h"

/* Free-running SysTick used to measure the ISR work. Delay_Us() cannot be used
 * here because the WCH implementation takes ownership of the same counter. */
#define STK_CTLR    (*(volatile uint32_t *)0xE000F000u)
#define STK_SR      (*(volatile uint32_t *)0xE000F004u)
#define STK_CNT32   (*(volatile uint32_t *)0xE000F008u)
#define STK_CNT64   (*(volatile uint64_t *)0xE000F008u)
#define STK_CMP64   (*(volatile uint64_t *)0xE000F010u)
#define STK_FREERUN ((1u << 0) | (1u << 2) | (1u << 3))
#define WORK_GUARD  2000000u

static volatile uint32_t Work_Ticks;

void TIM2_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));

void TIM2_IRQHandler(void)
{
    TIM_ClearITPendingBit(TIM2, TIM_IT_Update);
    /* Keep the timer ISR setup identical in the 0 us and delayed images. */
    STK_CTLR = STK_FREERUN;

#if IRQ_WORK_US > 0
    {
        uint32_t start, guard = 0;
        const uint32_t n = Work_Ticks;

        start = STK_CNT32;
        while ((uint32_t)(STK_CNT32 - start) < n)
        {
            if (++guard > WORK_GUARD)
            {
                break;
            }
        }
    }
#endif
}

void CompetingIRQ_Init(void)
{
    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure = { 0 };
    NVIC_InitTypeDef NVIC_InitStructure = { 0 };

    STK_CTLR = 0;
    STK_SR = 0;
    STK_CMP64 = (uint64_t)-1;
    STK_CNT64 = 0;
    STK_CTLR = STK_FREERUN;
    Work_Ticks = (uint32_t)IRQ_WORK_US * (SystemCoreClock / 1000000u);

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);
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
