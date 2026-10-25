// Offline quote lifecycle tests: every native call is a stub; no WeChat access.
#include <cstdlib>
#include <string>
#include "../native/text_bridge.cpp"

#define CHECK(value) do { if (!(value)) { std::fprintf(stderr, "check failed at %d\n", __LINE__); return 1; } } while (0)
static QuoteConfig config{};
static unsigned char payload[0x2e8]{}, message[0x798]{};
static LONG control[4]{};
static int lookups, builds, sends, reference_destroys, typed_destroys, account_checks;
static int change_account_at, fail_stage;
static bool found, complete, wrong_chat, native_failure, cleanup_failure;

static void view(void* object, size_t offset, const char* text) {
    auto value = reinterpret_cast<SmallString*>(static_cast<unsigned char*>(object) + offset);
    *value = {};
    value->length = std::strlen(text);
    value->capacity = value->length > 15 ? value->length : 15;
    if (value->capacity > 15) *reinterpret_cast<const char**>(value->data) = text;
    else std::memcpy(value->data, text, value->length + 1);
}
static void maybe_fail(int stage) {
    if (stage == fail_stage) {
        ULONG_PTR info[] = {0, 0x28};
        RaiseException(EXCEPTION_ACCESS_VIOLATION, 0, 2, info);
    }
}
static SharedMessage* __fastcall context(SharedMessage* out) {
    maybe_fail(90); *out = {reinterpret_cast<void*>(1), reinterpret_cast<void*>(2)}; return out;
}
static SharedMessage* __fastcall provider(void*, SharedMessage* out) {
    maybe_fail(91); *out = {reinterpret_cast<void*>(3), reinterpret_cast<void*>(4)}; return out;
}
static SharedMessage* __fastcall manager(void*, SharedMessage* out) {
    maybe_fail(92); *out = {reinterpret_cast<void*>(5), reinterpret_cast<void*>(6)}; return out;
}
static void* __fastcall lookup(void*, void* out, const SmallString*, uint64_t id, uint32_t mode) {
    ++lookups; maybe_fail(93);
    if (mode || id != config.reference_id) std::abort();
    auto data = static_cast<unsigned char*>(out);
    if (found) {
        *reinterpret_cast<uintptr_t*>(data) = core_base + 0x8f71468;
        *reinterpret_cast<uint64_t*>(data + 0x148) = id;
        data[0x2e8] = 1;
        std::memcpy(payload, data, sizeof(payload));
    }
    return out;
}
static void* __fastcall materialize(void* out, const void*) {
    maybe_fail(94);
    auto data = static_cast<unsigned char*>(out);
    *reinterpret_cast<uintptr_t*>(data) = core_base + 0x8f704b8;
    *reinterpret_cast<uint64_t*>(data + 8) = 1;
    *reinterpret_cast<uint64_t*>(data + 0xb8) = config.reference_id;
    view(data, 0x10, config.author);
    view(data, 0x30, !std::strcmp(config.author, config.account) ? config.base.target : config.account);
    view(data, 0x50, config.author);
    view(data, 0x70, wrong_chat ? "wxid_wrong_chat" : config.base.target);
    view(data, 0xe0, config.original);
    if (complete) *reinterpret_cast<SharedMessage*>(data + 0x2c0) = {payload, control};
    return out;
}
static SharedMessage* __fastcall build(SharedMessage* out, const void* source, const SmallString*, const void*) {
    ++builds; maybe_fail(96);
    std::memset(message, 0, sizeof(message));
    *reinterpret_cast<uintptr_t*>(message) = core_address(core_base, 0x8e53588);
    *reinterpret_cast<uint64_t*>(message + 0x118) = 0x3900000031ull;
    std::memcpy(message + 0x248, source, 0x440);
    message[0x688] = 1;
    control[2] = 1;
    *out = {message, control}; return out;
}
static void* __fastcall send(void*, void* out, SharedMessage* transferred, uint8_t flag) {
    ++sends;
    if (flag != 1 || transferred->object != message || control[2] != 2) std::abort();
    --control[2]; *transferred = {};
    maybe_fail(98);
    *static_cast<uint64_t*>(out) = native_failure ? 1ull << 32 : 0;
    return out;
}
static void __fastcall destroy_reference(void*) { ++reference_destroys; }
static void __fastcall destroy_typed(void*) { ++typed_destroys; }
static bool account(const SharedMessage&, const char*) {
    ++account_checks; return account_checks != change_account_at;
}
static bool set_string(SmallString* value, const char* text) { view(value, 0, text); return true; }
static void free_string(SmallString*, TestResult*) { }
static void release(SharedMessage* value, TestResult* report) {
    if (value->object == message) --control[2];
    *value = {};
    if (cleanup_failure) report->exception = 123;
}
static QuoteNativeApi api{context, provider, manager, lookup, materialize, build, send,
    destroy_reference, destroy_typed, account, set_string, free_string, release};
static void reset() {
    lookups = builds = sends = reference_destroys = typed_destroys = account_checks = 0;
    change_account_at = fail_stage = 0;
    found = complete = true; wrong_chat = native_failure = cleanup_failure = false;
    config.send = 0;
    std::memset(control, 0, sizeof(control));
}
static QuoteOutcome run() { QuoteOutcome out{}; quote_execute(config, api, &out); return out; }

int main() {
    // Independent frozen .56-only addresses; reject unknown profiles AND .13.
    const Core56Target targets[] = {Core56Target::QuoteReferenceVtable, Core56Target::LookupMessage,
        Core56Target::MaterializeReference, Core56Target::BuildQuote,
        Core56Target::DestroyReference, Core56Target::DestroyTyped};
    const uintptr_t expected[] = {0x8f704b8, 0x17da070, 0x183b690, 0x3a1b050, 0x1b64f0, 0x1cc640};
    for (LONG profile : {0, 1, 2, 3}) {
        InterlockedExchange(&core_profile_id, profile);
        for (size_t i = 0; i < 6; ++i) {
            CHECK(core56_rva(targets[i]) == (profile == 2 ? expected[i] : 0));
            CHECK(core56_address(0x100000, targets[i]) == (profile == 2 ? 0x100000 + expected[i] : 0));
        }
        CHECK(!core56_address(0x100000, static_cast<Core56Target>(-1)));
    }
    core_base = 0x100000; InterlockedExchange(&core_profile_id, 2);
    config.base.base.size = sizeof(config); config.base.base.pid = GetCurrentProcessId();
    strcpy_s(config.account, "wxid_test_self"); strcpy_s(config.author, "wxid_test_peer");
    strcpy_s(config.base.target, "wxid_test_peer"); strcpy_s(config.original, "original");
    strcpy_s(config.base.base.text, "reply"); config.reference_id = 3918779855605843848ull;
    CHECK(quote_config_valid(config));

    reset(); auto out = run();
    CHECK(!out.status && out.source_complete && out.built && out.action.stage == 97);
    CHECK(!out.send_called && !out.sent && !sends && lookups == 1 && builds == 1);
    CHECK(reference_destroys == 1 && typed_destroys == 1 && control[2] == 0);

    reset(); config.send = 1; out = run();
    CHECK(!out.status && out.sent && out.send_called && sends == 1 && out.action.stage == 99);
    CHECK(reference_destroys == 1 && typed_destroys == 1 && control[2] == 0);

    reset(); config.send = 1; native_failure = true; out = run();
    CHECK(out.status && !out.sent && out.send_called && out.action.native_status_high == 1 && control[2] == 0);

    reset(); found = false; config.send = 1; out = run();
    CHECK(out.status == ERROR_NOT_FOUND && !out.source_found && !sends && !builds);
    CHECK(!reference_destroys && !typed_destroys);

    reset(); complete = false; config.send = 1; out = run();
    CHECK(out.status == ERROR_INVALID_DATA && out.source_found && !out.source_complete && !builds && !sends);
    CHECK(reference_destroys == 1 && typed_destroys == 1);

    reset(); wrong_chat = true; config.send = 1; out = run();
    CHECK(out.status == ERROR_INVALID_DATA && !builds && !sends);
    for (int check = 1; check <= 4; ++check) {
        reset(); change_account_at = check; config.send = 1; out = run();
        CHECK(out.status == ERROR_NOT_READY && !out.send_called && !sends);
    }
    for (int stage : {90, 91, 92, 93, 94, 96, 98}) {
        reset(); fail_stage = stage; config.send = 1; cleanup_failure = true; out = run();
        CHECK(out.status && out.action.exception == EXCEPTION_ACCESS_VIOLATION && out.action.stage == static_cast<DWORD>(stage));
        CHECK(out.action.fault_address == 0x28 && out.cleanup.exception == 123);
        CHECK(out.send_called == (stage == 98) && !out.sent && control[2] == 0);
    }
    reset(); run();
    auto reference = message + 0x248;
    CHECK(quote_reference_valid(reference, config));
    *reinterpret_cast<uint64_t*>(payload + 0x148) += 1;
    CHECK(!quote_reference_valid(reference, config));
    *reinterpret_cast<uint64_t*>(payload + 0x148) -= 1;
    std::string longer = "original but not the same message";
    view(reference, 0xe0, longer.c_str());
    CHECK(!quote_reference_valid(reference, config));
    std::string capped(1000, 'a'); strcpy_s(config.original, capped.c_str());
    longer = capped + " rest of the original"; view(reference, 0xe0, longer.c_str());
    CHECK(quote_reference_valid(reference, config));
    strcpy_s(config.author, config.account); materialize(reference, nullptr);
    CHECK(quote_reference_valid(reference, config));
    std::puts("native quote lifecycle: probe/no-send, owned lookup, scope, cleanup and stage/fault checks passed; NO WeChat calls");
    return 0;
}
