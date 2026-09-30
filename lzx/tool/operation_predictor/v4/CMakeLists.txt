cmake_minimum_required(VERSION 3.18)
project(rankprop_single LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Torch REQUIRED)

add_executable(rankprop_example rankprop_single_all_in_one.cpp)
target_link_libraries(rankprop_example PRIVATE "${TORCH_LIBRARIES}")
set_property(TARGET rankprop_example PROPERTY CXX_STANDARD 20)
