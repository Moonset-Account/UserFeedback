CXX ?= clang++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
LDFLAGS ?=

BUILD_DIR := build
SRC_DIR := src
TEST_DIR := tests

LIB_SOURCES := $(SRC_DIR)/json.cpp $(SRC_DIR)/model.cpp $(SRC_DIR)/loader.cpp \
               $(SRC_DIR)/engine.cpp $(SRC_DIR)/compare.cpp $(SRC_DIR)/report.cpp
LIB_OBJECTS := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD_DIR)/%.o,$(LIB_SOURCES))

BIN := $(BUILD_DIR)/memtriage
TEST_BIN := $(BUILD_DIR)/memtriage_tests

.PHONY: all clean test run-demo

all: $(BIN)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) -c $< -o $@

$(BIN): $(LIB_OBJECTS) $(BUILD_DIR)/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

$(TEST_BIN): $(LIB_OBJECTS) $(BUILD_DIR)/tests.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

$(BUILD_DIR)/tests.o: $(TEST_DIR)/tests.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -I$(SRC_DIR) -I$(TEST_DIR) -c $< -o $@

test: $(TEST_BIN)
	./$(TEST_BIN)

# 端到端演示：当前数据集 + 与基线对比，输出文本与 JSON 两份报告。
run-demo: $(BIN)
	./$(BIN) --input data/current --baseline data/baseline --out build/report.txt
	./$(BIN) --input data/current --baseline data/baseline --json --out build/report.json
	@echo "报告已生成: build/report.txt, build/report.json"

clean:
	rm -rf $(BUILD_DIR)
