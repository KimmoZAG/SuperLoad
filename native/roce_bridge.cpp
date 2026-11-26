#include <infiniband/verbs.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <random>
#include <string>

namespace {
thread_local std::string error_text;
using Clock = std::chrono::steady_clock;
constexpr unsigned depth = 2;

int fail(const char *what, int error) {
    error_text = std::string(what) + ": " + std::strerror(error);
    return -1;
}

struct Endpoint {
    ibv_context *context = nullptr;
    ibv_pd *pd = nullptr;
    ibv_cq *cq = nullptr;
    ibv_qp *qp = nullptr;
    ibv_mr *mr = nullptr;
    void *buffer = nullptr;
    uint32_t psn = 0;
    union ibv_gid gid{};
    int gid_index = 0;
    uint8_t port = 1;
    uint32_t chunk = 0;
    size_t receive_bytes = 0;

    ~Endpoint() {
        if (qp) ibv_destroy_qp(qp);
        if (mr) ibv_dereg_mr(mr);
        if (cq) ibv_destroy_cq(cq);
        if (pd) ibv_dealloc_pd(pd);
        if (context) ibv_close_device(context);
        std::free(buffer);
    }
};

int init(Endpoint &ep, const char *device_name) {
    ibv_device **devices = ibv_get_device_list(nullptr);
    if (!devices) return fail("ibv_get_device_list", errno);
    errno = 0;
    for (auto **it = devices; *it; ++it) {
        if (std::strcmp(ibv_get_device_name(*it), device_name) == 0) {
            ep.context = ibv_open_device(*it);
            break;
        }
    }
    const int open_error = errno;
    ibv_free_device_list(devices);
    if (!ep.context) return fail("ibv_open_device", open_error ? open_error : ENODEV);

    ibv_port_attr port{};
    const int query = ibv_query_port(ep.context, ep.port, &port);
    if (query) return fail("ibv_query_port", query);
    if (port.link_layer != IBV_LINK_LAYER_ETHERNET || port.state != IBV_PORT_ACTIVE)
        return fail("active Ethernet/RoCE port required", ENETDOWN);
    const int gid = ibv_query_gid(ep.context, ep.port, ep.gid_index, &ep.gid);
    if (gid) return fail("ibv_query_gid", gid);
    union ibv_gid zero{};
    if (std::memcmp(&ep.gid, &zero, sizeof(zero)) == 0) return fail("empty GID slot", EINVAL);

    ep.pd = ibv_alloc_pd(ep.context);
    if (!ep.pd) return fail("ibv_alloc_pd", errno);
    ep.cq = ibv_create_cq(ep.context, 8, nullptr, nullptr, 0);
    if (!ep.cq) return fail("ibv_create_cq", errno);
    const size_t window = ep.chunk * depth;
    const int alloc = posix_memalign(&ep.buffer, 4096, window);
    if (alloc) return fail("posix_memalign", alloc);
    std::memset(ep.buffer, 0, window);
    ep.mr = ibv_reg_mr(ep.pd, ep.buffer, window, IBV_ACCESS_LOCAL_WRITE);
    if (!ep.mr) return fail("ibv_reg_mr (check memlock)", errno);
    ibv_qp_init_attr create{};
    create.send_cq = ep.cq;
    create.recv_cq = ep.cq;
    create.cap.max_send_wr = depth;
    create.cap.max_recv_wr = depth;
    create.cap.max_send_sge = 1;
    create.cap.max_recv_sge = 1;
    create.qp_type = IBV_QPT_RC;
    ep.qp = ibv_create_qp(ep.pd, &create);
    if (!ep.qp) return fail("ibv_create_qp", errno);
    ibv_qp_attr attr{};
    attr.qp_state = IBV_QPS_INIT;
    attr.port_num = ep.port;
    const int modify = ibv_modify_qp(ep.qp, &attr, IBV_QP_STATE | IBV_QP_PKEY_INDEX |
                                    IBV_QP_PORT | IBV_QP_ACCESS_FLAGS);
    if (modify) return fail("ibv_modify_qp INIT", modify);
    ep.psn = std::random_device{}() & 0xffffffu;
    return 0;
}

unsigned char *window(Endpoint &ep, unsigned slot) {
    return static_cast<unsigned char *>(ep.buffer) + slot * ep.chunk;
}

int post(Endpoint &ep, unsigned slot, uint32_t size, bool send) {
    ibv_sge entry{reinterpret_cast<uintptr_t>(window(ep, slot)), size, ep.mr->lkey};
    if (send) {
        ibv_send_wr wr{}, *bad = nullptr;
        wr.wr_id = slot;
        wr.sg_list = &entry;
        wr.num_sge = 1;
        wr.opcode = IBV_WR_SEND;
        wr.send_flags = IBV_SEND_SIGNALED;
        const int rc = ibv_post_send(ep.qp, &wr, &bad);
        return rc ? fail("ibv_post_send", rc) : 0;
    }
    ibv_recv_wr wr{}, *bad = nullptr;
    wr.wr_id = slot;
    wr.sg_list = &entry;
    wr.num_sge = 1;
    const int rc = ibv_post_recv(ep.qp, &wr, &bad);
    return rc ? fail("ibv_post_recv", rc) : 0;
}

int completion(Endpoint &ep, unsigned slot, ibv_wc_opcode expected, uint32_t size, Clock::time_point deadline) {
    while (Clock::now() < deadline) {
        ibv_wc wc{};
        const int n = ibv_poll_cq(ep.cq, 1, &wc);
        if (n < 0) return fail("ibv_poll_cq", EIO);
        if (n == 0) continue;
        if (wc.status != IBV_WC_SUCCESS) {
            error_text = std::string("RoCE completion: ") + ibv_wc_status_str(wc.status);
            return -1;
        }
        if (wc.wr_id != slot || wc.opcode != expected || (expected == IBV_WC_RECV && wc.byte_len != size))
            return fail("unexpected RoCE completion", EPROTO);
        return 0;
    }
    return fail("RoCE completion deadline", ETIMEDOUT);
}
}  // namespace

extern "C" {
const char *sl_roce_error() {
    return error_text.c_str();
}

void *sl_roce_open(const char *device, unsigned port, int gid_index, unsigned chunk) {
    try {
        if (port < 1 || port > 255 || gid_index < 0 || gid_index > 255 || chunk < 8192 || chunk % 8192) {
            fail("invalid RoCE configuration", EINVAL);
            return nullptr;
        }
        auto ep = std::make_unique<Endpoint>();
        ep->port = port;
        ep->gid_index = gid_index;
        ep->chunk = chunk / depth;
        return init(*ep, device) == 0 ? ep.release() : nullptr;
    } catch (const std::exception &error) {
        error_text = error.what();
        return nullptr;
    }
}

void sl_roce_address(void *handle, uint32_t *qpn, uint32_t *psn, unsigned char *gid) {
    auto &ep = *static_cast<Endpoint *>(handle);
    *qpn = ep.qp->qp_num;
    *psn = ep.psn;
    std::memcpy(gid, ep.gid.raw, sizeof(ep.gid.raw));
}

int sl_roce_connect(void *handle, uint32_t qpn, uint32_t psn, const unsigned char *gid) {
    auto &ep = *static_cast<Endpoint *>(handle);
    ibv_qp_attr attr{};
    attr.qp_state = IBV_QPS_RTR;
    attr.path_mtu = IBV_MTU_1024;
    attr.dest_qp_num = qpn;
    attr.rq_psn = psn;
    attr.max_dest_rd_atomic = 1;
    attr.min_rnr_timer = 1;
    attr.ah_attr.is_global = 1;
    attr.ah_attr.port_num = ep.port;
    attr.ah_attr.grh.sgid_index = ep.gid_index;
    attr.ah_attr.grh.hop_limit = 64;
    std::memcpy(attr.ah_attr.grh.dgid.raw, gid, sizeof(attr.ah_attr.grh.dgid.raw));
    int rc = ibv_modify_qp(ep.qp, &attr, IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
                           IBV_QP_DEST_QPN | IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER);
    if (rc) return fail("ibv_modify_qp RTR", rc);
    attr = {};
    attr.qp_state = IBV_QPS_RTS;
    attr.timeout = 14;
    attr.retry_cnt = 7;
    attr.rnr_retry = 7;
    attr.sq_psn = ep.psn;
    attr.max_rd_atomic = 1;
    rc = ibv_modify_qp(ep.qp, &attr, IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
                       IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC);
    return rc ? fail("ibv_modify_qp RTS", rc) : 0;
}

int sl_roce_prepare_recv(void *handle, size_t size) {
    auto &ep = *static_cast<Endpoint *>(handle);
    if (!size || ep.receive_bytes) return fail("invalid receive preparation", EINVAL);
    for (unsigned slot = 0; slot < depth && slot * ep.chunk < size; ++slot) {
        const auto amount = std::min<size_t>(ep.chunk, size - slot * ep.chunk);
        if (post(ep, slot, amount, false)) return -1;
    }
    ep.receive_bytes = size;
    return 0;
}

int sl_roce_transfer(void *handle, unsigned char *data, size_t size, int send, int timeout_ms) {
    auto &ep = *static_cast<Endpoint *>(handle);
    const auto deadline = Clock::now() + std::chrono::milliseconds(timeout_ms);
    if (send) {
        for (unsigned slot = 0; slot < depth && slot * ep.chunk < size; ++slot) {
            const size_t offset = slot * ep.chunk;
            const auto amount = std::min<size_t>(ep.chunk, size - offset);
            std::memcpy(window(ep, slot), data + offset, amount);
            if (post(ep, slot, amount, true)) return -1;
        }
    } else if (size != ep.receive_bytes) {
        return fail("receive not prepared for payload", EINVAL);
    }
    for (size_t offset = 0; offset < size; offset += ep.chunk) {
        const unsigned slot = (offset / ep.chunk) % depth;
        const uint32_t amount = std::min<size_t>(ep.chunk, size - offset);
        if (completion(ep, slot, send ? IBV_WC_SEND : IBV_WC_RECV, amount, deadline)) return -1;
        if (!send) std::memcpy(data + offset, window(ep, slot), amount);
        const size_t next = offset + depth * ep.chunk;
        if (next < size) {
            const auto next_amount = std::min<size_t>(ep.chunk, size - next);
            if (send) std::memcpy(window(ep, slot), data + next, next_amount);
            if (post(ep, slot, next_amount, send)) return -1;
        }
    }
    if (!send) ep.receive_bytes = 0;
    return 0;
}

void sl_roce_close(void *handle) {
    delete static_cast<Endpoint *>(handle);
}
}
