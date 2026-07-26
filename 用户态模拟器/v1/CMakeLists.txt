cmake_minimum_required(VERSION 3.16)
project(myself_kswapd_simulator VERSION 1.0 LANGUAGES C)

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

option(RECLAIM_ENABLE_TESTS "Build CTest tests" ON)
option(RECLAIM_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_compile_options(-Wall -Wextra -Wpedantic -Werror)

if(RECLAIM_ENABLE_SANITIZERS)
    include(CheckCSourceCompiles)
    set(CMAKE_REQUIRED_FLAGS "-fsanitize=address,undefined")
    set(CMAKE_REQUIRED_LINK_OPTIONS "-fsanitize=address,undefined")
    check_c_source_compiles("int main(void) { return 0; }" RECLAIM_HAS_SANITIZERS)
    unset(CMAKE_REQUIRED_FLAGS)
    unset(CMAKE_REQUIRED_LINK_OPTIONS)
    if(NOT RECLAIM_HAS_SANITIZERS)
        message(FATAL_ERROR "Compiler does not support AddressSanitizer and UndefinedBehaviorSanitizer")
    endif()
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
endif()

include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

add_library(reclaim_core STATIC
    src/core/list.c
    src/core/types.c
    src/core/engine.c
    src/core/domain.c
    src/core/page.c
    src/core/lru.c
    src/core/hash.c
    src/core/stats.c
    src/core/aging_g1.c
    src/core/scan_pressure.c
    src/core/reclaim.c
    src/core/validator.c
)
target_include_directories(reclaim_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_library(reclaim_userspace STATIC
    src/simulator/event_parser.c
    src/simulator/event_runner.c
    src/simulator/userspace_platform.c
    src/simulator/simulator_executor.c
)
target_link_libraries(reclaim_userspace PUBLIC reclaim_core)
target_include_directories(reclaim_userspace PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(reclaim_simulator src/simulator/main.c)
target_link_libraries(reclaim_simulator PRIVATE reclaim_userspace reclaim_core)

if(RECLAIM_ENABLE_TESTS)
    enable_testing()
    add_executable(reclaim_tests
        tests/test_support/test.c
        tests/unit/test_list.c
        tests/unit/test_types.c
        tests/unit/test_engine.c
        tests/unit/test_policy.c
        tests/integration/test_reclaim.c
        tests/integration/test_executor_outcomes.c
        tests/integration/test_reclaim_failures.c
        tests/integration/test_validation_corruption.c
        tests/scenarios/test_trace.c
    )
    target_link_libraries(reclaim_tests PRIVATE reclaim_userspace reclaim_core)
    add_test(NAME reclaim_tests COMMAND reclaim_tests)
endif()
