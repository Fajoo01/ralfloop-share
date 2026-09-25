cmake_minimum_required(VERSION 3.22.1)
project(bottazzi_native C)

add_library(
    bottazzi_native
    SHARED
    bottazzi_core.c
    native_jni.c
)

target_compile_features(bottazzi_native PRIVATE c_std_11)
target_compile_options(
    bottazzi_native
    PRIVATE
    -Wall
    -Wextra
    -Werror
    -O2
    -fvisibility=hidden
)
