SHELL := /bin/sh

SDK_COMMIT := 69a2eec903b4f919fcb73d1ab6c10c690780e4d1
TOOLCHAIN_COMMIT_DARWIN := e6360e0f77854a50a653a38ddbde4af413b06453
TOOLCHAIN_COMMIT_LINUX := fde267fb356efc11dee4c48ea58a1fd6dc787603

TOOLCHAIN_DIR ?= .toolchain
CROSS := $(TOOLCHAIN_DIR)/bin/riscv-wch-elf-
CC := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump
SIZE := $(CROSS)size

# Microseconds spent in the competing TIM2 ISR. This is the experiment's only knob.
WORK_US ?= 0
POLICY ?= vanilla
POLICIES := vanilla no-tog-ok nyet

ifeq ($(filter $(POLICY),$(POLICIES)),)
$(error POLICY must be one of: $(POLICIES))
endif

SDK_SRC := sdk/EVT/EXAM/SRC
VENDOR := sdk/EVT/EXAM/USB/USBHS/DEVICE/CH372Device/User
BUILD := build-$(POLICY)-$(WORK_US)us
ELF := $(BUILD)/ch372-echo.elf
BIN := $(BUILD)/firmware.bin
LINKER := $(SDK_SRC)/Ld/Link.ld

ifeq ($(POLICY),vanilla)
MAIN_SOURCE := src/main.c
USB_SOURCE := src/ch32v30x_usbhs_device.c
else
POLICY_PATCH := patches/$(POLICY).patch
PATCHED := $(BUILD)/patched
MAIN_SOURCE := $(PATCHED)/main.c
USB_SOURCE := $(PATCHED)/ch32v30x_usbhs_device.c
endif

ARCH := -march=rv32imacxw -mabi=ilp32
CFLAGS := -std=gnu99 -Os -g -Wall -Wextra -Wno-unused-parameter \
	-msmall-data-limit=8 -msave-restore -fmessage-length=0 -fsigned-char \
	-ffunction-sections -fdata-sections -fno-common $(ARCH) \
	-DCH32V30x_D8C -DIRQ_WORK_US=$(WORK_US) \
	-Isrc -I$(SDK_SRC)/Core -I$(SDK_SRC)/Peripheral/inc \
	-I$(SDK_SRC)/Debug -I$(VENDOR)
LDFLAGS := -T $(LINKER) -Os -g $(ARCH) -ffunction-sections -fdata-sections \
	-Wl,-gc-sections --specs=nano.specs --specs=nosys.specs -nostartfiles \
	-Wl,-Map=$(BUILD)/ch372-echo.map

SRC := $(MAIN_SOURCE) $(USB_SOURCE) src/usb_desc.c \
	src/ch32v30x_it.c src/competing_irq.c $(VENDOR)/system_ch32v30x.c \
	$(SDK_SRC)/Core/core_riscv.c $(SDK_SRC)/Debug/debug.c \
	$(SDK_SRC)/Startup/startup_ch32v30x_D8C.S \
	$(SDK_SRC)/Peripheral/src/ch32v30x_rcc.c \
	$(SDK_SRC)/Peripheral/src/ch32v30x_misc.c \
	$(SDK_SRC)/Peripheral/src/ch32v30x_gpio.c \
	$(SDK_SRC)/Peripheral/src/ch32v30x_usart.c \
	$(SDK_SRC)/Peripheral/src/ch32v30x_tim.c
OBJ := $(addprefix $(BUILD)/,$(addsuffix .o,$(notdir $(SRC))))

VENDOR_IDENTICAL := ch32v30x_conf.h ch32v30x_it.c ch32v30x_it.h \
	ch32v30x_usbhs_device.c ch32v30x_usbhs_device.h usb_desc.c usb_desc.h

.DEFAULT_GOAL := all
.DELETE_ON_ERROR:
.PHONY: all build clean help check check-sdk check-toolchain check-vendor check-patch toolchain

help:
	@printf '%s\n' \
		'Usage:' \
		'  make                         Build the four documented firmware images' \
		'  make build [POLICY=name] [WORK_US=N]' \
		'                               Build one firmware image' \
		'  make toolchain               Fetch the pinned WCH GCC 12.2.0 toolchain' \
		'  make check                   Verify SDK, toolchain, vendor files and patch' \
		'  make clean                   Remove generated build directories' \
		'' \
		'Parameters for make build:' \
		'  POLICY=vanilla               Unmodified WCH EP1 OUT handling (default)' \
		'  POLICY=no-tog-ok              Accept EP1 OUT without checking TOG_OK' \
		'  POLICY=nyet                   Keep TOG_OK and arm EP1 OUT with NYET' \
		'  WORK_US=N                     Work in the competing TIM2 ISR, in us (default: 0)' \
		'' \
		'Output:' \
		'  build-<policy>-<N>us/firmware.bin'

build: check $(BIN)
	@echo "policy: $(POLICY), competing ISR work: $(WORK_US) us"
	@$(SIZE) -A $(ELF) | awk '$$1 ~ /^\.(text|data|bss)$$/ {printf "  %-6s %6d\n", $$1, $$2}'
	@shasum -a 256 $(BIN)

all:
	$(MAKE) POLICY=vanilla WORK_US=0 build
	$(MAKE) POLICY=vanilla WORK_US=5 build
	$(MAKE) POLICY=no-tog-ok WORK_US=5 build
	$(MAKE) POLICY=nyet WORK_US=5 build

check: check-sdk check-toolchain check-vendor check-patch

check-sdk:
	@test "$$(git -C sdk rev-parse HEAD 2>/dev/null)" = "$(SDK_COMMIT)" || { \
		echo "sdk/: missing or wrong commit; run: git submodule update --init"; exit 1; }
	@test -z "$$(git -C sdk status --short)" || { echo "sdk/: dirty submodule"; exit 1; }

check-toolchain:
	@test -x "$(CC)" || { echo "WCH GCC not found; run: make toolchain"; exit 1; }
	@test "$$($(CC) -dumpfullversion)" = "12.2.0" || { \
		echo "expected WCH GCC 12.2.0"; exit 1; }
	@case "$$(uname -s)" in \
		Darwin) expected=$(TOOLCHAIN_COMMIT_DARWIN) ;; \
		Linux) expected=$(TOOLCHAIN_COMMIT_LINUX) ;; \
		*) echo "unsupported build host: $$(uname -s)"; exit 1 ;; \
	esac; \
	test "$$(git -C "$(TOOLCHAIN_DIR)" rev-parse HEAD 2>/dev/null)" = "$$expected" || { \
		echo "toolchain revision mismatch"; exit 1; }

check-vendor:
	@for file in $(VENDOR_IDENTICAL); do \
		cmp -s "src/$$file" "$(VENDOR)/$$file" || { \
			echo "src/$$file differs from the WCH example"; exit 1; }; \
	done
	@echo "WCH USB source: 7 files byte-identical"

check-patch:
ifneq ($(POLICY),vanilla)
	@command -v patch >/dev/null || { echo "patch command not found"; exit 1; }
endif

toolchain:
	@set -eu; \
	case "$$(uname -s):$$(uname -m)" in \
		Darwin:arm64|Darwin:x86_64) \
			url=https://github.com/Community-PIO-CH32V/toolchain-riscv-mac.git; \
			commit=$(TOOLCHAIN_COMMIT_DARWIN) ;; \
		Linux:x86_64|Linux:amd64) \
			url=https://github.com/Community-PIO-CH32V/toolchain-riscv-linux.git; \
			commit=$(TOOLCHAIN_COMMIT_LINUX) ;; \
		*) echo "unsupported build host: $$(uname -s) $$(uname -m)"; exit 1 ;; \
	esac; \
	if test -e "$(TOOLCHAIN_DIR)"; then \
		test "$$(git -C "$(TOOLCHAIN_DIR)" rev-parse HEAD 2>/dev/null)" = "$$commit" || { \
			echo "$(TOOLCHAIN_DIR) exists at the wrong revision"; exit 1; }; \
	else \
		git init "$(TOOLCHAIN_DIR)"; \
		git -C "$(TOOLCHAIN_DIR)" remote add origin "$$url"; \
		git -C "$(TOOLCHAIN_DIR)" fetch --depth=1 origin "$$commit"; \
		git -C "$(TOOLCHAIN_DIR)" checkout --detach "$$commit"; \
	fi
	@$(CC) --version | sed -n '1p'

$(BIN): $(ELF)
	$(OBJCOPY) -O binary $< $@
	$(OBJDUMP) -M xw -d $< > $(BUILD)/ch372-echo.lst

$(ELF): $(OBJ) $(LINKER)
	$(CC) -o $@ $(LDFLAGS) $(OBJ) -lm

ifneq ($(POLICY),vanilla)
$(MAIN_SOURCE): src/main.c src/ch32v30x_usbhs_device.c $(POLICY_PATCH) | $(BUILD)
	@mkdir -p $(PATCHED)
	@cp src/main.c src/ch32v30x_usbhs_device.c $(PATCHED)/
	@patch --batch --forward -s -p1 -d $(PATCHED) < $(POLICY_PATCH)

$(USB_SOURCE): $(MAIN_SOURCE)
	@test -f $@
endif

define rule
$(BUILD)/$(notdir $(1)).o: $(1) | $(BUILD)
	$$(CC) -o $$@ -c $$(CFLAGS) $$<
endef
$(foreach source,$(SRC),$(eval $(call rule,$(source))))

$(BUILD):
	@mkdir -p $@

clean:
	@rm -rf -- $(wildcard build-*-*us)
