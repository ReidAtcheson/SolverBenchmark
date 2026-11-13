cmake_minimum_required(VERSION 3.24)
project(SolverBenchmark VERSION 0.1.0 LANGUAGES CXX)
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "The worker transport currently requires Linux")
endif()
include(FetchContent)
include(GNUInstallDirs)
include(CMakePackageConfigHelpers)
option(SOLVER_BENCHMARK_BUILD_EIGEN "Build the Eigen SparseLU worker" ON)
option(SOLVER_BENCHMARK_BUILD_SUPERLU "Build the SuperLU worker" OFF)
option(SOLVER_BENCHMARK_BUILD_TESTS "Build worker fixtures and register tests" ON)
FetchContent_Declare(mdspan URL https://codeload.github.com/kokkos/mdspan/tar.gz/b15adf7aa6626b231a1aa01a4d2f8ba558a24527
  URL_HASH SHA256=451dd02213bf6225c80b14ea69b9b681a5a15d4de266c104407ea154488e939d
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE SOURCE_SUBDIR unused)
FetchContent_Declare(json URL https://codeload.github.com/nlohmann/json/tar.gz/9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03
  URL_HASH SHA256=0dbc5e40a01ff142e7e68c03e85247a4dcede2f592d12d3677dee3664d17975a
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE SOURCE_SUBDIR unused)
FetchContent_MakeAvailable(mdspan json)
add_library(solver_benchmark_headers INTERFACE)
add_library(SolverBenchmark::headers ALIAS solver_benchmark_headers)
set_target_properties(solver_benchmark_headers PROPERTIES EXPORT_NAME headers)
target_compile_features(solver_benchmark_headers INTERFACE cxx_std_20)
target_compile_definitions(solver_benchmark_headers INTERFACE SOLVER_BENCHMARK_USE_KOKKOS_MDSPAN)
target_include_directories(solver_benchmark_headers INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<BUILD_INTERFACE:${mdspan_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
add_library(solver_benchmark_worker STATIC src/worker.cpp)
add_library(SolverBenchmark::worker ALIAS solver_benchmark_worker)
set_target_properties(solver_benchmark_worker PROPERTIES EXPORT_NAME worker)
target_link_libraries(solver_benchmark_worker PUBLIC solver_benchmark_headers)
target_include_directories(solver_benchmark_worker SYSTEM PRIVATE ${json_SOURCE_DIR}/single_include)
target_compile_options(solver_benchmark_worker PRIVATE -Wall -Wextra -Wpedantic)
if(SOLVER_BENCHMARK_BUILD_EIGEN)
  FetchContent_Declare(eigen URL https://codeload.github.com/eigen-mirror/eigen/tar.gz/3147391d946bb4b6c68edd901f2add6ac1f31f8c
    URL_HASH SHA256=0c8c490764f9c2a793133491adca0cd073b73e0bde965c68cbe58d91b5ed4261
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE SOURCE_SUBDIR unused)
  FetchContent_MakeAvailable(eigen)
  add_executable(sb-eigen adapters/eigen/main.cpp)
  target_link_libraries(sb-eigen PRIVATE SolverBenchmark::worker)
  target_include_directories(sb-eigen SYSTEM PRIVATE ${eigen_SOURCE_DIR})
  target_compile_definitions(sb-eigen PRIVATE EIGEN_MPL2_ONLY EIGEN_DONT_PARALLELIZE)
  install(FILES ${eigen_SOURCE_DIR}/Eigen/src/OrderingMethods/Eigen_Colamd.h DESTINATION ${CMAKE_INSTALL_DATADIR}/SolverBenchmark/licenses RENAME Eigen-COLAMD-source-and-notice.h)
  install(FILES ${eigen_SOURCE_DIR}/COPYING.MPL2 DESTINATION ${CMAKE_INSTALL_DATADIR}/SolverBenchmark/licenses RENAME Eigen-COPYING.MPL2)
  install(TARGETS sb-eigen RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
if(SOLVER_BENCHMARK_BUILD_SUPERLU)
  find_package(superlu CONFIG REQUIRED)
  add_executable(sb-superlu adapters/superlu/main.cpp)
  target_link_libraries(sb-superlu PRIVATE SolverBenchmark::worker superlu::superlu)
  # Preserve non-system dependency locations when installing the worker. This
  # allows a SuperLU installation discovered via CMAKE_PREFIX_PATH to load.
  set_target_properties(sb-superlu PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)
  install(TARGETS sb-superlu RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()
install(TARGETS solver_benchmark_headers solver_benchmark_worker EXPORT SolverBenchmarkTargets
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(DIRECTORY ${mdspan_SOURCE_DIR}/include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(FILES ${mdspan_SOURCE_DIR}/LICENSE DESTINATION ${CMAKE_INSTALL_DATADIR}/SolverBenchmark/licenses RENAME mdspan-LICENSE)
install(FILES ${json_SOURCE_DIR}/LICENSE.MIT DESTINATION ${CMAKE_INSTALL_DATADIR}/SolverBenchmark/licenses RENAME json-LICENSE)
install(FILES LICENSE THIRD_PARTY_NOTICES.md DESTINATION ${CMAKE_INSTALL_DATADIR}/SolverBenchmark)
install(EXPORT SolverBenchmarkTargets NAMESPACE SolverBenchmark:: DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/SolverBenchmark)
configure_package_config_file(cmake/SolverBenchmarkConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/SolverBenchmarkConfig.cmake INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/SolverBenchmark)
write_basic_package_version_file(${CMAKE_CURRENT_BINARY_DIR}/SolverBenchmarkConfigVersion.cmake VERSION ${PROJECT_VERSION} COMPATIBILITY SameMajorVersion)
install(FILES ${CMAKE_CURRENT_BINARY_DIR}/SolverBenchmarkConfig.cmake ${CMAKE_CURRENT_BINARY_DIR}/SolverBenchmarkConfigVersion.cmake DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/SolverBenchmark)
if(SOLVER_BENCHMARK_BUILD_TESTS)
  enable_testing()
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_executable(sb-worker-fixture tests/worker_fixture.cpp)
  target_link_libraries(sb-worker-fixture PRIVATE SolverBenchmark::worker)
  add_test(NAME python-tests COMMAND ${Python3_EXECUTABLE} -m unittest discover -s ${CMAKE_CURRENT_SOURCE_DIR}/tests -v)
  set_tests_properties(python-tests PROPERTIES ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/python:$ENV{PYTHONPATH};SB_WORKER_FIXTURE=$<TARGET_FILE:sb-worker-fixture>;SB_EIGEN_WORKER=${CMAKE_CURRENT_BINARY_DIR}/sb-eigen")
  if(SOLVER_BENCHMARK_BUILD_SUPERLU)
    set_property(TEST python-tests APPEND PROPERTY ENVIRONMENT "SB_SUPERLU_WORKER=$<TARGET_FILE:sb-superlu>")
    add_test(NAME superlu-installed-worker
      COMMAND ${CMAKE_COMMAND}
        -DBUILD_DIR=${CMAKE_CURRENT_BINARY_DIR}
        -DINSTALL_DIR=${CMAKE_CURRENT_BINARY_DIR}/test-install-superlu
        -DINSTALL_BINDIR=${CMAKE_INSTALL_BINDIR}
        -DBUILD_CONFIG=$<CONFIG>
        -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/check_superlu_install.cmake)
  endif()
endif()
