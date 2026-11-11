# fmtrewrite build. Uses the system Clang at runtime (no LLVM libraries are
# linked against; clang is driven as a subprocess and its JSON AST is parsed).

CXX      ?= c++
CXXSTD   := -std=c++17
WARNINGS := -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
OPT      := -O2

SRC := src/main.cpp src/Rewriter.cpp src/Json.cpp src/Diff.cpp
BIN := build/fmtrewrite

.PHONY: all clean test

all: $(BIN)

$(BIN): $(SRC) src/*.hpp
	@mkdir -p build
	$(CXX) $(CXXSTD) $(WARNINGS) $(OPT) $(SRC) -o $(BIN)

clean:
	rm -rf build

test: $(BIN)
	./tests/run_tests.sh
