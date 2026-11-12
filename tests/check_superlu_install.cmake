# Exercise the installed binary without a loader path inherited from the test
# environment. A successful --describe requires all linked libraries to load.
execute_process(
  COMMAND "${CMAKE_COMMAND}" --install "${BUILD_DIR}" --prefix "${INSTALL_DIR}" --config "${BUILD_CONFIG}"
  RESULT_VARIABLE install_result OUTPUT_QUIET ERROR_VARIABLE install_error)
if(NOT install_result EQUAL 0)
  message(FATAL_ERROR "Installation failed: ${install_error}")
endif()
execute_process(
  COMMAND "${CMAKE_COMMAND}" -E env --unset=LD_LIBRARY_PATH
          "${INSTALL_DIR}/${INSTALL_BINDIR}/sb-superlu" --describe
  RESULT_VARIABLE describe_result OUTPUT_VARIABLE identity ERROR_VARIABLE describe_error)
if(NOT describe_result EQUAL 0)
  message(FATAL_ERROR "Installed worker failed to start: ${describe_error}")
endif()
string(JSON worker_name ERROR_VARIABLE identity_error GET "${identity}" name)
if(identity_error OR NOT worker_name STREQUAL "superlu")
  message(FATAL_ERROR "Installed worker returned invalid identity: ${identity}")
endif()
