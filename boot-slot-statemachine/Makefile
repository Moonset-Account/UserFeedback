CXX ?= clang++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Wpedantic -O2

SRC := src/main.cpp src/state_machine.cpp src/persistent_store.cpp
OUT := build/boot-slot-sim

.PHONY: all run clean selftest

all: $(OUT)

$(OUT): $(SRC) include/bootmgr.hpp | build
	$(CXX) $(CXXFLAGS) -Iinclude $(SRC) -o $(OUT)

build:
	mkdir -p build

run: all
	$(OUT) scenarios

selftest: all
	$(OUT) --selftest scenarios

clean:
	rm -rf build
