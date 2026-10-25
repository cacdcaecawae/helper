#pragma once
// 本机 UI 手动发送的普通文字：复用已验证 SendFunction，不改消息或调用参数。
// Hook 内只复制字段到已有队列，不写文件、不访问数据库，不发送任何新消息。

struct ManualTextSnapshot {
    bool present;
    uint32_t type;
    bool structured_quote;
    char receiver[128];
    char body[262144];
    char quote_layout[4096];
    char quote_reply[1024];
};

static bool quote_json_string(char* output, size_t limit, size_t* used, const char* value) {
    if (*used + 2 >= limit) return false;
    output[(*used)++] = '"';
    for (const unsigned char* cursor = reinterpret_cast<const unsigned char*>(value); *cursor; ++cursor) {
        if (*used + 7 >= limit) return false;
        if (*cursor == '"' || *cursor == '\\') { output[(*used)++] = '\\'; output[(*used)++] = *cursor; }
        else if (*cursor < 32) {
            *used += static_cast<size_t>(sprintf_s(output + *used, limit - *used, "\\u%04x", *cursor));
        } else output[(*used)++] = *cursor;
    }
    output[(*used)++] = '"'; output[*used] = 0;
    return true;
}

static bool copy_manual_string(const void* object, size_t offset, char* output, size_t limit) {
    __try {
        const auto value = reinterpret_cast<const SmallString*>(
            static_cast<const unsigned char*>(object) + offset);
        if (!value->length || value->length >= limit || value->length > value->capacity ||
            !(value->capacity == 15 || (value->capacity >= 16 && value->capacity <= 1024 * 1024))) return false;
        const char* bytes = value->capacity > 15 ?
            *reinterpret_cast<const char* const*>(value->data) : value->data;
        // NUL 不应使一条实际长消息被误识别成截断的短消息。
        if (std::memchr(bytes, 0, static_cast<size_t>(value->length))) return false;
        std::memcpy(output, bytes, static_cast<size_t>(value->length));
        output[value->length] = 0;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void snapshot_private_quote_text(const unsigned char* object, ManualTextSnapshot* snapshot) {
    // Exact .56 private-text layout confirmed against the user's native card
    // AND its final serialized refermsg. Groups/non-text fail closed for now.
    if (InterlockedCompareExchange(&core_profile_id, 0, 0) != 2 ||
        std::strstr(snapshot->receiver, "@chatroom") || object[0x688] != 1) return;
    const auto reference = object + 0x248;
    if (*reinterpret_cast<const uintptr_t*>(reference) != core56_address(core_base, Core56Target::QuoteReferenceVtable) ||
        *reinterpret_cast<const uint64_t*>(reference + 8) != 1) return;
    char author[128]{}, chat[128]{}, original[16384]{}, reply[16384]{};
    if (!copy_manual_string(reference, 0x50, author, sizeof(author)) || !valid_target(author) ||
        !copy_manual_string(reference, 0x70, chat, sizeof(chat)) || std::strcmp(chat, snapshot->receiver) ||
        !copy_manual_string(reference, 0xe0, original, sizeof(original)) ||
        !copy_manual_string(object, 0x758, reply, sizeof(reply))) return;
    const uint64_t identifier = *reinterpret_cast<const uint64_t*>(reference + 0xb8);
    if (!identifier) return;
    size_t used = static_cast<size_t>(sprintf_s(snapshot->body, "{\"version\":1,\"reply\":"));
    if (!quote_json_string(snapshot->body, sizeof(snapshot->body), &used, reply)) return;
    used += static_cast<size_t>(sprintf_s(snapshot->body + used, sizeof(snapshot->body) - used,
        ",\"reference\":{\"message_id\":\"%llu\",\"sender_name\":\"好友\",\"sender_id\":",
        static_cast<unsigned long long>(identifier)));
    if (!quote_json_string(snapshot->body, sizeof(snapshot->body), &used, author)) return;
    used += static_cast<size_t>(sprintf_s(snapshot->body + used, sizeof(snapshot->body) - used, ",\"type\":1,\"content\":"));
    if (!quote_json_string(snapshot->body, sizeof(snapshot->body), &used, original)) return;
    strcpy_s(snapshot->body + used, sizeof(snapshot->body) - used, "}}");
    snapshot->structured_quote = true;
}

// The common app-message constructor initializes these std::string fields.
// Read only this verified layout, never arbitrary pointers or an object dump.
// Both supported builds use a 0x758-byte app object (text adds 0x40 bytes).
static void snapshot_manual_quote(const unsigned char* object, ManualTextSnapshot* snapshot) {
    const uintptr_t vtable = *reinterpret_cast<const uintptr_t*>(object);
    const bool text_layout = vtable == core_address(core_base, 0x8e53588);
    const bool known = text_layout || vtable == core_address(core_base, 0x8e53718);
    const uintptr_t image_size = InterlockedCompareExchange(&core_profile_id, 0, 0) == 2 ?
        0xc34f000 : 0xc0ae000;
    if (vtable < core_base || vtable - core_base >= image_size) return;
    if (!copy_manual_string(object, 0xb0, snapshot->receiver, sizeof(snapshot->receiver)) ||
        !valid_target(snapshot->receiver)) return;
    auto scratch = static_cast<char*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(snapshot->body)));
    if (!scratch) return;
    __try {
        size_t used = static_cast<size_t>(sprintf_s(snapshot->quote_layout,
            "{\"vtable_rva\":%llu,\"known_layout\":%s,\"fields\":[",
            static_cast<unsigned long long>(vtable - core_base), known ? "true" : "false"));
        size_t xml_offset = 0;
        bool ambiguous = false, comma = false;
        if (known) {
            const size_t offsets[] = {0x18, 0x38, 0x58, 0x78, 0x148, 0x188, 0x1a8, 0x1d0, 0x6f8, 0x718, 0x738};
            for (const size_t offset : offsets) {
                if (!copy_manual_string(object, offset, scratch, sizeof(snapshot->body))) continue;
                used += static_cast<size_t>(sprintf_s(snapshot->quote_layout + used,
                    sizeof(snapshot->quote_layout) - used, "%s{\"offset\":%llu,\"length\":%llu}",
                    comma ? "," : "", static_cast<unsigned long long>(offset),
                    static_cast<unsigned long long>(std::strlen(scratch))));
                comma = true;
                // Only the actual quote XML may enter the ordinary receive path.
                // Other fields (paths, tokens, unrelated text) remain unobserved.
                if (!std::strstr(scratch, "<appmsg") || !std::strstr(scratch, "<refermsg")) continue;
                if (xml_offset && std::strcmp(snapshot->body, scratch)) ambiguous = true;
                if (!xml_offset) { strcpy_s(snapshot->body, scratch); xml_offset = offset; }
            }
        }
        if (ambiguous) { snapshot->body[0] = 0; xml_offset = 0; }
        // Static verification on .56: 0x3a1b050 creates a TEXT object for type
        // 49/57, reply at +0x758; the referenced message is an embedded value
        // at +0x248 (presence flag +0x688), not an XML string. Do not mistake
        // that referenced message's own XML for the newly sent reply.
        size_t reply_length = 0;
        bool reference_present = false;
        if (text_layout) {
            if (copy_manual_string(object, 0x758, scratch, sizeof(snapshot->body))) {
                reply_length = std::strlen(scratch);
                if (reply_length < sizeof(snapshot->quote_reply)) strcpy_s(snapshot->quote_reply, scratch);
            }
            reference_present = object[0x688] == 1;
            if (reference_present) {
                const size_t reference_offsets[] = {0x10, 0x30, 0x50, 0x70, 0x98, 0xe0, 0x100,
                    0x140, 0x160, 0x188, 0x1a8, 0x1c8, 0x1e8, 0x208, 0x228};
                for (const size_t relative : reference_offsets) {
                    const size_t offset = 0x248 + relative;
                    if (!copy_manual_string(object, offset, scratch, sizeof(snapshot->body))) continue;
                    used += static_cast<size_t>(sprintf_s(snapshot->quote_layout + used,
                        sizeof(snapshot->quote_layout) - used, "%s{\"offset\":%llu,\"length\":%llu}",
                        comma ? "," : "", static_cast<unsigned long long>(offset),
                        static_cast<unsigned long long>(std::strlen(scratch))));
                    comma = true;
                }
            }
        }
        sprintf_s(snapshot->quote_layout + used, sizeof(snapshot->quote_layout) - used,
            "],\"xml_offset\":%llu,\"ambiguous\":%s,\"reply_length\":%llu,\"reference_present\":%s}",
            static_cast<unsigned long long>(xml_offset), ambiguous ? "true" : "false",
            static_cast<unsigned long long>(reply_length), reference_present ? "true" : "false");
        snapshot->type = 49;
        snapshot->present = true;
        if (text_layout) snapshot_private_quote_text(object, snapshot);
    } __finally {
        SecureZeroMemory(scratch, sizeof(snapshot->body));
        HeapFree(GetProcessHeap(), 0, scratch);
    }
}

static void snapshot_manual_text(SharedMessage* message, ManualTextSnapshot* snapshot) {
    __try {
        if (!message || !message->object || !snapshot) return;
        const auto object = static_cast<const unsigned char*>(message->object);
        const uint64_t type = *reinterpret_cast<const uint64_t*>(object + 0x118);
        if (type == 0x3900000031ull) { snapshot_manual_quote(object, snapshot); return; }
        if (*reinterpret_cast<const uintptr_t*>(object) != core_address(core_base, 0x8e53588) ||
            static_cast<uint32_t>(type) != 1) return;
        snapshot->type = 1;
        snapshot->present = copy_manual_string(object, 0xb0, snapshot->receiver, sizeof(snapshot->receiver)) &&
            valid_target(snapshot->receiver) &&
            copy_manual_string(object, 0x758, snapshot->body, sizeof(snapshot->body));
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

// Acceptance precedes the server acknowledgement. +0x148 is a string on this
// object, not a server ID; never publish its pointer/SSO bytes as an identifier.
static uint64_t manual_text_message_id(void*) { return 0; }

static void publish_manual_text(void* output, const ManualTextSnapshot* snapshot) {
    __try {
        if (!output || !snapshot || !snapshot->present || *static_cast<const uint64_t*>(output) != 0) return;
        if (snapshot->type == 49) {
            // 32 = quote-layout metadata, numbers/booleans only. No guessed server ID:
            // the app object's +0x148 is a string, not the text-result ID field.
            enqueue_received("self", snapshot->receiver, snapshot->quote_reply, snapshot->quote_layout, 49,
                snapshot->body, 8u | 32u | (snapshot->structured_quote ? 64u : 0u), 0);
            return;
        }
        const uint64_t message_id = manual_text_message_id(output);
        // 8 = 本机发出；账号由 Python 再核对当前登录身份后填入，不能猜 sender 偏移。
        // 16 = 普通文字超过旧 text[1024] 边界，使用 V5 的大缓冲区，不改变 ABI。
        const bool large = std::strlen(snapshot->body) >= sizeof(ReceivedMessage::text);
        enqueue_received("self", snapshot->receiver, large ? "" : snapshot->body, nullptr, 1,
            large ? snapshot->body : nullptr, 8u | (large ? 16u : 0u), message_id);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static void* __fastcall manual_text_send(void* manager, void* output, SharedMessage* message, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    auto snapshot = static_cast<ManualTextSnapshot*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(ManualTextSnapshot)));
    void* result = nullptr;
    __try {
        snapshot_manual_text(message, snapshot);
        // 原函数拥有的 SharedMessage 引用可被消耗；返回后只用之前复制的字段及结果对象。
        result = original_send(manager, output, message, flag);
        publish_manual_text(output, snapshot);
    } __finally {
        if (snapshot) {
            SecureZeroMemory(snapshot, sizeof(*snapshot));
            HeapFree(GetProcessHeap(), 0, snapshot);
        }
        _InterlockedDecrement(&active_callbacks);
    }
    return result;
}

static DWORD manual_text_receive_start() {
    if (hook_created) return ERROR_BUSY;
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x178f100));
    MH_STATUS status = MH_CreateHook(target, reinterpret_cast<void*>(&manual_text_send), reinterpret_cast<void**>(&original_send));
    if (status != MH_OK) return 600 + status;
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); return 600 + status; }
    // 复用公共 hook 生命周期；WxRemoveSelfTest 会禁用、等待回调并移除此入口。
    hook_created = true;
    return ERROR_SUCCESS;
}
