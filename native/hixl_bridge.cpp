#include <hixl/hixl.h>

#include <cstdint>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <string>
#include <vector>

namespace {
struct Endpoint {
    hixl::Hixl engine;
    hixl::MemHandle memory = nullptr;
    std::string peer;
};
}  // namespace

extern "C" {
void *sl_hixl_open(const char *local, const char *comm_res_path, unsigned *status) {
    try {
        auto endpoint = std::make_unique<Endpoint>();
        std::map<hixl::AscendString, hixl::AscendString> options;
        if (*comm_res_path) {
            std::ifstream in(comm_res_path);
            if (!in) {
                *status = hixl::PARAM_INVALID;
                return nullptr;
            }
            std::string config((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
            options.emplace(hixl::OPTION_LOCAL_COMM_RES, config.c_str());
        }
        *status = endpoint->engine.Initialize(local, options);
        return *status == hixl::SUCCESS ? endpoint.release() : nullptr;
    } catch (...) {
        *status = hixl::FAILED;
        return nullptr;
    }
}

unsigned sl_hixl_register(void *handle, uintptr_t address, size_t size, int device) {
    auto &endpoint = *static_cast<Endpoint *>(handle);
    hixl::MemDesc mem{address, size};
    return endpoint.engine.RegisterMem(mem, device ? hixl::MEM_DEVICE : hixl::MEM_HOST, endpoint.memory);
}

unsigned sl_hixl_connect(void *handle, const char *peer, int timeout_ms) {
    auto &endpoint = *static_cast<Endpoint *>(handle);
    const auto status = endpoint.engine.Connect(peer, timeout_ms);
    if (status == hixl::SUCCESS)
        endpoint.peer = peer;
    return status;
}

unsigned sl_hixl_read(void *handle, uintptr_t local, uintptr_t remote, size_t size, int timeout_ms) {
    auto &endpoint = *static_cast<Endpoint *>(handle);
    const std::vector<hixl::TransferOpDesc> transfers{{local, remote, size}};
    return endpoint.engine.TransferSync(endpoint.peer.c_str(), hixl::READ, transfers, timeout_ms);
}

void sl_hixl_close(void *handle) {
    const std::unique_ptr<Endpoint> endpoint(static_cast<Endpoint *>(handle));
    if (!endpoint->peer.empty())
        endpoint->engine.Disconnect(endpoint->peer.c_str(), 3000);
    if (endpoint->memory != nullptr)
        endpoint->engine.DeregisterMem(endpoint->memory);
    endpoint->engine.Finalize();
}
}
