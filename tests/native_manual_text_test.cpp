// 合成消息对象验证本机发送采样；original_send 是桩函数，不加载或调用微信。
#include <cstdlib>
#include <string>
#include "../native/text_bridge.cpp"

#define CHECK(value) do { if (!(value)) { std::fprintf(stderr, "check failed at %d\n", __LINE__); return 1; } } while (0)
static unsigned char object_storage[0x798]{};
static uint32_t fake_status = 0;
static uint32_t fake_status_high = 0;
static int calls = 0;
static bool arguments_preserved = true;

static void* __fastcall fake_send(void* manager, void* output, SharedMessage* input, uint8_t flag) {
    ++calls;
    arguments_preserved = arguments_preserved && manager == reinterpret_cast<void*>(123) && flag == 7;
    *static_cast<uint32_t*>(output) = fake_status;
    *reinterpret_cast<uint32_t*>(static_cast<unsigned char*>(output) + 4) = fake_status_high;
    *reinterpret_cast<SharedMessage*>(static_cast<unsigned char*>(output) + 40) = {input->object, nullptr};
    *input = {}; // 模拟原函数消耗输入引用；回调不能再从输入对象指针取正文。
    return output;
}

static void string_view_at(size_t offset, const std::string& text) {
    auto value = reinterpret_cast<SmallString*>(object_storage + offset);
    *value = {};
    value->length = text.size();
    value->capacity = text.size() > 15 ? text.size() : 15;
    if (text.size() > 15) *reinterpret_cast<const char**>(value->data) = text.c_str();
    else std::memcpy(value->data, text.data(), text.size());
}

int main() {
    InterlockedExchange(&core_profile_id, 1);
    core_base = 0x100000;
    original_send = &fake_send;
    std::string target = "12345678@chatroom", body = u8"本机手动发的文字";
    auto received = std::make_unique<ReceivedMessage>();
    alignas(16) unsigned char result[128]{};
    auto send = [&]() {
        SharedMessage input{object_storage, nullptr};
        manual_text_send(reinterpret_cast<void*>(123), result, &input, 7);
        poll_received(received.get());
    };
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53588);
    *reinterpret_cast<uint32_t*>(object_storage + 0x118) = 1;
    *reinterpret_cast<uint64_t*>(object_storage + 0x148) = 42;
    string_view_at(0xb0, target); string_view_at(0x758, body);
    send();
    CHECK(received->present && received->type == 1 && received->reserved == 8);
    CHECK(std::string(received->text) == body && std::string(received->receiver) == target);
    CHECK(received->native_message_id == 0 && active_callbacks == 0 && arguments_preserved);
    fake_status = 5; send(); CHECK(!received->present); fake_status = 0;
    fake_status_high = 1; send(); CHECK(!received->present); fake_status_high = 0;
    *reinterpret_cast<uint32_t*>(object_storage + 0x118) = 3;
    send(); CHECK(!received->present);
    *reinterpret_cast<uint32_t*>(object_storage + 0x118) = 1;
    *reinterpret_cast<uintptr_t*>(object_storage) = 0;
    send(); CHECK(!received->present);
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53588);
    body = std::string(9000, 'x'); string_view_at(0x758, body);
    send(); CHECK(received->present && received->reserved == 24 && !received->text[0]);
    CHECK(std::string(received->image_xml) == body);
    auto value = reinterpret_cast<SmallString*>(object_storage + 0x758);
    value->capacity = 15; send(); CHECK(!received->present);
    std::memset(object_storage, 0, sizeof(object_storage));
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53718);
    *reinterpret_cast<uint64_t*>(object_storage + 0x118) = 0x3900000031ull;
    string_view_at(0xb0, target);
    body = "<msg><appmsg><type>57</type><title>111</title><refermsg><type>1</type></refermsg></appmsg></msg>";
    string_view_at(0x38, body);
    std::string unrelated = "synthetic-sensitive-path-or-token";
    string_view_at(0x188, unrelated);
    send();
    CHECK(received->present && received->type == 49 && received->reserved == 40);
    CHECK(!received->native_message_id && std::string(received->image_xml) == body);
    CHECK(std::strstr(received->mention_source, "\"xml_offset\":56"));
    CHECK(!std::strstr(received->mention_source, unrelated.c_str()));
    fake_status_high = 1; send(); CHECK(!received->present); fake_status_high = 0;
    *reinterpret_cast<uint64_t*>(object_storage + 0x118) = 0x600000031ull;
    send(); CHECK(!received->present); // files must not be sampled or downloaded
    *reinterpret_cast<uint64_t*>(object_storage + 0x118) = 0x3900000031ull;
    *reinterpret_cast<uintptr_t*>(object_storage) = core_base + 0x123456;
    send();
    CHECK(received->present && !received->image_xml[0]);
    CHECK(std::strstr(received->mention_source, "\"known_layout\":false"));
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53718);
    std::string different = "<appmsg>different<refermsg/></appmsg>";
    string_view_at(0x58, different);
    send(); CHECK(received->present && !received->image_xml[0]);
    CHECK(std::strstr(received->mention_source, "\"ambiguous\":true"));
    *reinterpret_cast<uintptr_t*>(object_storage) = 0;
    send(); CHECK(!received->present);
    std::memset(object_storage, 0, sizeof(object_storage));
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53588);
    *reinterpret_cast<uint64_t*>(object_storage + 0x118) = 0x3900000031ull;
    string_view_at(0xb0, target);
    body = "111"; string_view_at(0x758, body);
    object_storage[0x688] = 1;
    string_view_at(0x248 + 0x10, unrelated);
    send();
    CHECK(received->present && received->type == 49 && received->reserved == 40);
    CHECK(std::string(received->text) == "111" && !received->image_xml[0]);
    CHECK(std::strstr(received->mention_source, "\"reply_length\":3"));
    CHECK(std::strstr(received->mention_source, "\"reference_present\":true"));
    CHECK(!std::strstr(received->mention_source, unrelated.c_str()));
    InterlockedExchange(&core_profile_id, 2);
    *reinterpret_cast<uintptr_t*>(object_storage) = core_address(core_base, 0x8e53588);
    *reinterpret_cast<uintptr_t*>(object_storage + 0x248) = core_base + 0x8f704b8;
    *reinterpret_cast<uint64_t*>(object_storage + 0x248 + 8) = 1;
    *reinterpret_cast<uint64_t*>(object_storage + 0x248 + 0xb8) = 3918779855605843848ull;
    std::string peer = "wxid_synthetic_peer", original = "quote \"text\"\nsecond line";
    string_view_at(0xb0, peer); string_view_at(0x248 + 0x50, peer);
    string_view_at(0x248 + 0x70, peer); string_view_at(0x248 + 0xe0, original);
    send();
    CHECK(received->present && received->reserved == 104);
    CHECK(std::strstr(received->image_xml, "\"message_id\":\"3918779855605843848\""));
    CHECK(std::strstr(received->image_xml, "quote \\\"text\\\"\\u000asecond line"));
    auto config = std::make_unique<QuoteConfig>();
    config->base.base.size = sizeof(QuoteConfig); config->base.base.pid = GetCurrentProcessId();
    strcpy_s(config->base.target, peer.c_str()); strcpy_s(config->account, "wxid_synthetic_self");
    strcpy_s(config->author, peer.c_str()); strcpy_s(config->original, "original");
    strcpy_s(config->base.base.text, "reply"); config->reference_id = 42;
    CHECK(quote_config_valid(*config));
    strcpy_s(config->author, "wxid_another_peer"); CHECK(!quote_config_valid(*config));
    strcpy_s(config->author, peer.c_str()); strcpy_s(config->base.target, "12345678@chatroom");
    CHECK(!quote_config_valid(*config));
    CHECK(calls == 15 && arguments_preserved && active_callbacks == 0);
    std::puts("manual text/quote native checks: 15 send snapshots + quote validation passed; no WeChat calls");
    return 0;
}
