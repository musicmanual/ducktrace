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
