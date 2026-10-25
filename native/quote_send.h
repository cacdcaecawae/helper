#pragma once
// Exact .56 private-text quote path. The client's own 0x19fd480 resolves
// (chat, server ID) through 0x17da070, then materializes an owned MessageData
// through 0x183b690. A default MessageData populated with strings is NOT enough:
// refermsg serialization also consumes its nested typed message at +0x2c0.
// No heap scanning/borrowed pointers here, and no XML-as-text fallback.
struct QuoteConfig {
    TextConfig base;
    char account[128], author[128], original[16384];
    uint64_t reference_id;
    uint32_t created_at, send;
};
static_assert(sizeof(QuoteConfig) == 18344, "Unexpected quote ABI");
static_assert(offsetof(QuoteConfig, reference_id) == 18328, "Unexpected quote ID offset");

struct QuoteLease {
    QuoteConfig config;
    explicit QuoteLease(const QuoteConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~QuoteLease() { SecureZeroMemory(&config, sizeof(config)); _InterlockedDecrement(&live_tasks); }
};

static bool quote_config_valid(const QuoteConfig& config) {
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.send > 1 || !config.reference_id || !valid_target(config.base.target) ||
        !valid_target(config.account) || !valid_target(config.author) ||
        std::strstr(config.base.target, "@chatroom") || std::strstr(config.account, "@chatroom") ||
        (std::strcmp(config.author, config.account) && std::strcmp(config.author, config.base.target))) return false;
    const size_t reply = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    const size_t original = strnlen_s(config.original, sizeof(config.original));
    return reply && reply < sizeof(config.base.base.text) && original && original < sizeof(config.original) &&
        MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.base.text, static_cast<int>(reply), nullptr, 0) &&
        MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.original, static_cast<int>(original), nullptr, 0);
}

// Explicit call table also permits offline lifecycle tests. Production addresses
// are used only after quote_schedule has checked the exact .56 file fingerprint.
struct QuoteNativeApi {
    SharedMessage* (__fastcall* context)(SharedMessage*);
    SharedMessage* (__fastcall* provider)(void*, SharedMessage*);
    SharedMessage* (__fastcall* manager)(void*, SharedMessage*);
    void* (__fastcall* lookup)(void*, void*, const SmallString*, uint64_t, uint32_t);
    void* (__fastcall* materialize)(void*, const void*);
    SharedMessage* (__fastcall* build)(SharedMessage*, const void*, const SmallString*, const void*);
    SendFunction send;
    void (__fastcall* destroy_reference)(void*);
    void (__fastcall* destroy_typed)(void*);
    bool (*account_matches)(const SharedMessage&, const char*);
    bool (*set_string)(SmallString*, const char*);
    void (*free_string)(SmallString*, TestResult*);
    void (*release)(SharedMessage*, TestResult*);
};

struct QuoteOutcome {
    TestResult action{}, cleanup{};
    DWORD status = ERROR_GEN_FAILURE;
    bool source_found = false, source_complete = false;
    bool built = false, send_called = false, sent = false;
};

static bool quote_string_matches(const unsigned char* object, size_t offset, const char* expected,
                                 bool bounded_body = false) {
    const auto value = reinterpret_cast<const SmallString*>(object + offset);
    const size_t length = std::strlen(expected);
    if (!length || value->length < length || value->length > 262143 || value->capacity < value->length ||
        !(value->capacity == 15 || (value->capacity >= 16 && value->capacity <= 1024 * 1024))) return false;
    // Public quote snapshots cap the original at 1000 Unicode code points.
    // A shorter snapshot must match the entire original, not just a prefix.
    if (value->length != length) {
        size_t characters = 0;
        for (size_t i = 0; i < length; ++i)
            if ((static_cast<unsigned char>(expected[i]) & 0xc0) != 0x80) ++characters;
        if (!bounded_body || characters != 1000) return false;
    }
    const char* data = value->capacity == 15 ? value->data : *reinterpret_cast<const char* const*>(value->data);
    return data && !std::memcmp(data, expected, length) && data[value->length] == 0;
}

static bool quote_reference_valid(const unsigned char* reference, const QuoteConfig& config) {
    __try {
        if (*reinterpret_cast<const uintptr_t*>(reference) != core56_address(core_base, Core56Target::QuoteReferenceVtable) ||
            *reinterpret_cast<const uint64_t*>(reference + 8) != 1 ||
            *reinterpret_cast<const uint64_t*>(reference + 0xb8) != config.reference_id) return false;
        const auto typed = reinterpret_cast<const SharedMessage*>(reference + 0x2c0);
        if (!typed->object || !typed->control ||
            *static_cast<const uintptr_t*>(typed->object) != core_address(core_base, 0x8d9f958) ||
            *reinterpret_cast<const uint64_t*>(static_cast<const unsigned char*>(typed->object) + 0x148) != config.reference_id)
            return false;
        const char* receiver = !std::strcmp(config.author, config.account) ? config.base.target : config.account;
        return quote_string_matches(reference, 0x10, config.author) &&
            quote_string_matches(reference, 0x30, receiver) &&
            quote_string_matches(reference, 0x50, config.author) &&
            quote_string_matches(reference, 0x70, config.base.target) &&
            quote_string_matches(reference, 0xe0, config.original, true);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void quote_execute(const QuoteConfig& config, const QuoteNativeApi& api, QuoteOutcome* outcome) {
    SharedMessage context{}, provider{}, manager{}, message{};
    alignas(16) unsigned char typed[0x2f0]{}, reference[0x440]{}, selection[0x68]{}, result[128]{};
    SmallString reply{}, chat{}; reply.capacity = chat.capacity = 15;
    *reinterpret_cast<uint64_t*>(selection + 0x18) = 15;
    *reinterpret_cast<uint64_t*>(selection + 0x38) = 15;
    *reinterpret_cast<uint64_t*>(selection + 0x60) = 15;
    bool initialized = false;
    auto& report = outcome->action;
    auto& status = outcome->status;
    __try {
        __try {
            report.stage = 90;
            api.context(&context);
            if (!api.account_matches(context, config.account)) { status = ERROR_NOT_READY; __leave; }
            report.stage = 91;
            api.provider(context.object, &provider);
            if (!provider.object || !provider.control) { status = ERROR_NOT_READY; __leave; }
            report.stage = 92;
            api.manager(provider.object, &manager);
            if (!manager.object || !manager.control || !api.account_matches(context, config.account)) {
                status = ERROR_NOT_READY; __leave;
            }
            if (!api.set_string(&chat, config.base.target) || !api.set_string(&reply, config.base.base.text)) {
                status = ERROR_NOT_ENOUGH_MEMORY; __leave;
            }
            report.stage = 93;
            // Owned optional<TypedMessage>, presence byte +0x2e8. This is the
            // same lookup on the same scheduler used by the client's quote UI.
            api.lookup(manager.object, typed, &chat, config.reference_id, 0);
            if (typed[0x2e8] != 1) { status = ERROR_NOT_FOUND; __leave; }
            outcome->source_found = true;
            if (*reinterpret_cast<const uintptr_t*>(typed) != core_address(core_base, 0x8d9f958) ||
                *reinterpret_cast<const uint64_t*>(typed + 0x148) != config.reference_id) {
                status = ERROR_INVALID_DATA; __leave;
            }
            report.stage = 94;
            api.materialize(reference, typed); // fully owned MessageData, including its typed payload
            initialized = true;
            report.stage = 95;
            if (!quote_reference_valid(reference, config)) { status = ERROR_INVALID_DATA; __leave; }
            outcome->source_complete = true;
            if (!api.account_matches(context, config.account)) { status = ERROR_NOT_READY; __leave; }
            report.stage = 96;
            api.build(&message, reference, &reply, selection);
            if (!message.object || !message.control) { status = ERROR_INVALID_DATA; __leave; }
            const auto object = static_cast<unsigned char*>(message.object);
            if (*reinterpret_cast<uintptr_t*>(object) != core_address(core_base, 0x8e53588) ||
                *reinterpret_cast<uint64_t*>(object + 0x118) != 0x3900000031ull || object[0x688] != 1 ||
                !quote_reference_valid(object + 0x248, config) ||
                !api.set_string(reinterpret_cast<SmallString*>(object + 0xb0), config.base.target)) {
                status = ERROR_INVALID_DATA; __leave;
            }
            outcome->built = true;
            report.stage = 97;
            if (!api.account_matches(context, config.account)) { status = ERROR_NOT_READY; __leave; }
            if (!config.send) { status = ERROR_SUCCESS; __leave; } // probe NEVER calls send
            SharedMessage transferred = message;
            _InterlockedIncrement(reinterpret_cast<volatile LONG*>(static_cast<unsigned char*>(message.control) + 8));
            report.stage = 98;
            outcome->send_called = true;
            api.send(manager.object, result, &transferred, 1);
            report.stage = 99;
            report.native_status = *reinterpret_cast<uint32_t*>(result);
            report.native_status_high = *reinterpret_cast<uint32_t*>(result + 4);
            outcome->sent = !report.native_status && !report.native_status_high;
            status = outcome->sent ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
        } __except (record_exception(GetExceptionInformation(), &report)) { status = ERROR_GEN_FAILURE; }
    } __finally {
        // Keep cleanup faults separate: never overwrite the original failing PC.
        auto& cleanup = outcome->cleanup;
        api.release(reinterpret_cast<SharedMessage*>(result + 40), &cleanup);
        api.free_string(reinterpret_cast<SmallString*>(result + 8), &cleanup);
        api.release(&message, &cleanup);
        if (initialized) {
            __try { api.destroy_reference(reference); }
            __except (record_exception(GetExceptionInformation(), &cleanup)) { }
        }
        if (typed[0x2e8] == 1) {
            __try { api.destroy_typed(typed); }
            __except (record_exception(GetExceptionInformation(), &cleanup)) { }
        }
        api.free_string(&reply, &cleanup);
        api.free_string(&chat, &cleanup);
        api.release(&manager, &cleanup);
        api.release(&provider, &cleanup);
        api.release(&context, &cleanup);
        SecureZeroMemory(reference, sizeof(reference));
        SecureZeroMemory(typed, sizeof(typed));
    }
}

static void quote_on_task(const QuoteLease& lease) {
    const auto& config = lease.config;
    QuoteNativeApi api{};
    api.context = reinterpret_cast<decltype(api.context)>(core_address(core_base, 0x47220));
    api.provider = reinterpret_cast<decltype(api.provider)>(core_address(core_base, 0x36c740));
    api.manager = reinterpret_cast<decltype(api.manager)>(core_address(core_base, 0x6f4d90));
    api.lookup = reinterpret_cast<decltype(api.lookup)>(core56_address(core_base, Core56Target::LookupMessage));
    api.materialize = reinterpret_cast<decltype(api.materialize)>(core56_address(core_base, Core56Target::MaterializeReference));
    api.build = reinterpret_cast<decltype(api.build)>(core56_address(core_base, Core56Target::BuildQuote));
    api.send = reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100));
    api.destroy_reference = reinterpret_cast<decltype(api.destroy_reference)>(core56_address(core_base, Core56Target::DestroyReference));
    api.destroy_typed = reinterpret_cast<decltype(api.destroy_typed)>(core56_address(core_base, Core56Target::DestroyTyped));
    api.account_matches = emoticon_account_matches;
    api.set_string = set_native_string;
    api.free_string = image_free_string;
    api.release = guarded_release_shared;
    QuoteOutcome outcome{};
    _InterlockedIncrement(&active_callbacks);
    __try {
        quote_execute(config, api, &outcome);
        const auto& report = outcome.action;
        uint32_t timestamp = 0, image_size = 0;
        const bool in_core = report.exception_pc >= core_base &&
            core_image_identity(reinterpret_cast<HMODULE>(core_base), &timestamp, &image_size) &&
            report.exception_pc - core_base < image_size;
        FILE* log = nullptr;
        if (!_wfopen_s(&log, config.base.base.log_path, L"wb") && log) {
            std::fprintf(log, "{\"diagnostic_version\":2,\"operation\":\"%s\",\"message_type\":49,\"app_type\":57,"
                "\"built\":%s,\"sent\":%s,\"stage\":%lu,\"exception\":%lu,\"status\":%lu,"
                "\"source_found\":%s,\"source_complete\":%s,\"send_called\":%s,"
                "\"native_status\":%u,\"native_status_high\":%u,\"cleanup_exception\":%lu,"
                "\"exception_in_core\":%s,\"exception_rva\":\"0x%llx\",\"access_kind\":%llu,\"fault_null_page\":%s,"
                "\"completion_success\":%s,\"target\":", config.send ? "quote_send" : "quote_probe",
                outcome.built ? "true" : "false", outcome.sent ? "true" : "false", report.stage, report.exception, outcome.status,
                outcome.source_found ? "true" : "false", outcome.source_complete ? "true" : "false",
                outcome.send_called ? "true" : "false", report.native_status, report.native_status_high, outcome.cleanup.exception,
                in_core ? "true" : "false", in_core ? report.exception_pc - core_base : 0,
                report.access_kind, report.exception == EXCEPTION_ACCESS_VIOLATION && report.fault_address < 4096 ? "true" : "false",
                !outcome.status && !report.exception && !outcome.cleanup.exception && outcome.built &&
                    outcome.source_complete && (!config.send || outcome.sent) ? "true" : "false");
            write_json_string(log, config.base.target);
            std::fputs("}\n", log); std::fclose(log); // no bodies, original IDs or raw XML in reports
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD quote_copy_config(void* parameter, QuoteConfig* config) {
    __try { *config = *static_cast<const QuoteConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD quote_schedule(void* parameter, bool send) {
    if (!parameter || live_tasks || live_completions) return ERROR_BUSY;
    QuoteConfig config{};
    const DWORD copied = quote_copy_config(parameter, &config);
    if (copied) return copied;
    if (!quote_config_valid(config) || config.send != (send ? 1u : 0u)) return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || InterlockedCompareExchange(&core_profile_id, 0, 0) != 2)
        return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<QuoteLease>(config);
    SecureZeroMemory(&config, sizeof(config));
    std::function<void()> task = [lease]() { quote_on_task(*lease); };
    return schedule_task(lease->config.base.base, &task);
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectQuote(void* parameter) { return quote_schedule(parameter, false); }
extern "C" __declspec(dllexport) DWORD WINAPI WxSendQuote(void* parameter) { return quote_schedule(parameter, true); }
