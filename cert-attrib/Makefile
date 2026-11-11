# SPDX-License-Identifier: MIT
# 纯 C++17、无外部依赖的离线构建。
CXX      ?= clang++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Iinclude
SRC      := src/loader.cpp src/engine.cpp src/report.cpp
DATA     := data/acceptance.json

.PHONY: all test run clean

all: build/cert-attrib build/attrib_tests

build:
	mkdir -p build

build/cert-attrib: $(SRC) src/main.cpp | build
	$(CXX) $(CXXFLAGS) $(SRC) src/main.cpp -o $@

build/attrib_tests: $(SRC) tests/attrib_tests.cpp | build
	$(CXX) $(CXXFLAGS) $(SRC) tests/attrib_tests.cpp -o $@

test: build/attrib_tests
	./build/attrib_tests $(DATA)

run: build/cert-attrib
	./build/cert-attrib --data $(DATA) --json-out build/report.json --text

clean:
	rm -rf build
