#include "common/ducktrace.h"

#include <cstdio>

namespace
{
constexpr const char* TRACE_PATH = "/home/lainforall/ducktrace/duckstation_trace.txt";
}

void DuckTrace(const char* function_name)
{
  if (!function_name)
    return;

  FILE* file = std::fopen(TRACE_PATH, "a");
  if (!file)
    return;

  std::fprintf(file, "%s\n", function_name);
  std::fflush(file);
  std::fclose(file);
}


void DuckTraceInstruction(
	unsigned int address,
	unsigned int instruction_bits,
    const char* disassembly,
    const char* comment)
{
    FILE* file = std::fopen(TRACE_PATH, "a");
    if (!file)
        return;

    std::fprintf(
        file,
        "MY_LABEL: address=0x%08X, opcode=0x%08X, disassembly=%s, extra=%s\n",
        address,
        instruction_bits,
        disassembly ? disassembly : "",
        comment ? comment : "");

    std::fflush(file);
    std::fclose(file);
}
