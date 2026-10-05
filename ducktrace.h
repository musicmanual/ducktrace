#pragma once

// Lightweight function-entry tracing used by the DuckStation instrumentation
// injector.
//
// The implementation is in ducktrace.cpp.
// Do not include this header from ducktrace.cpp after adding the declaration
// below; ducktrace.cpp includes this header normally.

void DuckTrace(const char* function_name);
