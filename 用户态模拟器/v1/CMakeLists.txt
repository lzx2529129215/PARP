cmake_minimum_required(VERSION 3.16)
project(myself_kswapd_simulator VERSION 1.0 LANGUAGES C)

option(RECLAIM_ENABLE_TESTS "Build CTest tests" ON)
option(RECLAIM_ENABLE_SANITIZERS "Enable AddressSanitizer and UndefinedBehaviorSanitizer" OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_compile_options(-Wall -Wextra -Wpedantic -Werror)

if(RECLAIM_ENABLE_SANITIZERS)
    include(CheckCCompilerFlag)
    check_c_compiler_flag("-fsanitize=address,undefined" RECLAIM_HAS_SANITIZERS)
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
    src/simulator/userspace_platform.c
    src/simulator/simulator_executor.c
)
target_include_directories(reclaim_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_executable(reclaim_simulator src/simulator/main.c)
target_link_libraries(reclaim_simulator PRIVATE reclaim_core)

if(RECLAIM_ENABLE_TESTS)
    enable_testing()
    add_executable(reclaim_tests
        tests/test_support/test.c
        tests/unit/test_list.c
        tests/unit/test_types.c
        tests/unit/test_engine.c
    )
    target_link_libraries(reclaim_tests PRIVATE reclaim_core)
    add_test(NAME reclaim_tests COMMAND reclaim_tests)
endif()
