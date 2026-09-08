cmake_minimum_required(VERSION 3.20)
project(seksun_cnc_geometry VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_path(
    OpenCASCADE_INCLUDE_DIR
    NAMES STEPControl_Reader.hxx
    PATH_SUFFIXES opencascade
    REQUIRED
)

add_executable(occt-analyzer src/main.cpp)
target_include_directories(occt-analyzer PRIVATE ${OpenCASCADE_INCLUDE_DIR})
target_link_libraries(
    occt-analyzer
    PRIVATE
        TKSTEP TKSTEPAttr TKSTEP209 TKSTEPBase TKXSBase TKShHealing
        TKSTL TKMesh TKTopAlgo TKGeomAlgo TKBRep TKGeomBase TKG3d TKG2d
        TKMath TKernel
)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(occt-analyzer PRIVATE -Wall -Wextra -Wpedantic)
endif()

install(TARGETS occt-analyzer RUNTIME DESTINATION bin)
