cmake_minimum_required(VERSION 3.14)
project(ws_receiver_lifecycle LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

add_library(ws_receiver STATIC src/ws_receiver.cpp)
target_include_directories(ws_receiver PUBLIC include)
target_compile_options(ws_receiver PRIVATE -Wall -Wextra -Wpedantic -Wshadow -Wconversion)

enable_testing()
add_executable(test_lifecycle tests/test_lifecycle.cpp)
target_link_libraries(test_lifecycle PRIVATE ws_receiver)
target_compile_options(test_lifecycle PRIVATE -Wall -Wextra -Wpedantic -Wshadow -Wconversion)
add_test(NAME lifecycle COMMAND test_lifecycle)
