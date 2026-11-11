# SPDX-License-Identifier: MIT
CXX      := clang++
CXXFLAGS := -std=c++17 -Wall -Wextra -Wpedantic -Werror -O2

SRCS := src/main.cpp src/parser.cpp src/engine.cpp
BIN  := build/patchplan-validator

.PHONY: all test clean

all: $(BIN)

$(BIN): $(SRCS) src/*.h
	@mkdir -p build
	$(CXX) $(CXXFLAGS) $(SRCS) -o $(BIN)

test: $(BIN)
	bash tests/run_tests.sh

clean:
	rm -rf build
