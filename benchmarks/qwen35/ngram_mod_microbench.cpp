#include "ngram-mod.h"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

int main() {
    constexpr size_t n_match = 24;
    constexpr size_t entries = 4 * 1024 * 1024;
    constexpr size_t rounds = 5 * 1000 * 1000;
    common_ngram_mod pool(n_match, entries);
    std::vector<int32_t> tokens(n_match + 1);
    for (size_t i = 0; i < tokens.size(); ++i) tokens[i] = static_cast<int32_t>(i + 1);
    pool.add(tokens.data());
    volatile int32_t sink = 0;
    const auto started = std::chrono::steady_clock::now();
    for (size_t i = 0; i < rounds; ++i) sink ^= pool.get(tokens.data());
    const auto elapsed = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - started).count();
    std::printf(
        "{\"entries\":%zu,\"bytes\":%zu,\"n_match\":%zu,"
        "\"lookups\":%zu,\"seconds\":%.9f,\"lookup_ns\":%.3f,\"sink\":%d}\n",
        pool.size(), pool.size_bytes(), n_match, rounds, elapsed,
        elapsed * 1e9 / rounds, static_cast<int>(sink));
}
