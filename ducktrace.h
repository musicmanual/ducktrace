#pragma once

// Lightweight function-entry tracing used by the DuckStation instrumentation
// injector.
//
// The implementation is in ducktrace.cpp.

void DuckTrace(const char* function_name);

void DuckTraceInstruction(
    unsigned int address,
    unsigned int instruction_bits,
    const char* disassembly,
    const char* comment);
