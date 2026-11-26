cmake_minimum_required(VERSION 3.18)
project(SuperLoadRoCE LANGUAGES CXX)

find_path(VERBS_INCLUDE infiniband/verbs.h REQUIRED)
find_library(VERBS_LIBRARY ibverbs REQUIRED)
add_library(superload_roce SHARED roce_bridge.cpp)
target_compile_features(superload_roce PRIVATE cxx_std_17)
target_include_directories(superload_roce PRIVATE "${VERBS_INCLUDE}")
target_link_libraries(superload_roce PRIVATE "${VERBS_LIBRARY}")
