#ifndef __COMPETING_IRQ_H__
#define __COMPETING_IRQ_H__

/* TIM2 fires every 997 us at the same priority as USBHS. Its handler clears the
 * timer flag, then occupies the CPU for IRQ_WORK_US. */
#ifndef IRQ_WORK_US
#define IRQ_WORK_US 0
#endif

#define COMPETING_IRQ_PERIOD_US 997
#define COMPETING_IRQ_PREEMPT   0
#define COMPETING_IRQ_SUB       0

#if IRQ_WORK_US < 0
#error IRQ_WORK_US must be non-negative
#endif

#if IRQ_WORK_US >= COMPETING_IRQ_PERIOD_US
#error IRQ_WORK_US must be shorter than the competing interrupt period
#endif

void CompetingIRQ_Init(void);

#endif
