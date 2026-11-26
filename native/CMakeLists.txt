cmake_minimum_required(VERSION 3.16)
project(SuperLoadHixl LANGUAGES CXX)

set(CANN_ROOT "/usr/local/Ascend/ascend-toolkit/latest" CACHE PATH "CANN Toolkit location")
find_path(HIXL_INCLUDE hixl/hixl.h HINTS "${CANN_ROOT}/include" REQUIRED)
find_library(HIXL_LIBRARY cann_hixl HINTS "${CANN_ROOT}/lib64" REQUIRED)
find_library(METADEF_LIBRARY metadef HINTS "${CANN_ROOT}/lib64" REQUIRED)

add_library(superload_hixl SHARED hixl_bridge.cpp)
target_compile_features(superload_hixl PRIVATE cxx_std_17)
target_include_directories(superload_hixl PRIVATE "${HIXL_INCLUDE}")
target_link_libraries(superload_hixl PRIVATE "${HIXL_LIBRARY}" "${METADEF_LIBRARY}")
