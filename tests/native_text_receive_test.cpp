// Synthetic Sync AddMsg objects only. No injection, WeChat process or native send.
#include <string>
#include "../native/text_bridge.cpp"

#define CHECK(value) do { if (!(value)) { std::fprintf(stderr, "check failed at %d\n", __LINE__); return false; } } while (0)

struct PbField {
    SmallString value{};
    uintptr_t wrapper[2]{};
    void bind(unsigned char* message, size_t offset, const std::string& text) {
        value.length = text.size();
        value.capacity = text.size() > 15 ? text.size() : 15;
        if (text.size() > 15) *reinterpret_cast<const char**>(value.data) = text.c_str();
        else std::memcpy(value.data, text.data(), text.size());
        wrapper[1] = reinterpret_cast<uintptr_t>(&value);
        *reinterpret_cast<uintptr_t*>(message + offset) = reinterpret_cast<uintptr_t>(wrapper);
    }
};

static bool check_text(const std::string& text, bool group = false, bool invalid = false) {
    std::string sender = group ? "12345678@chatroom" : "wxid_synthetic_peer";
    std::string receiver = "wxid_synthetic_self";
    alignas(16) unsigned char wire[0x78]{};
    PbField from, to, content;
    from.bind(wire, 8, sender); to.bind(wire, 0x18, receiver); content.bind(wire, 0x20, text);
    if (invalid) content.value.capacity = 1;
    *reinterpret_cast<uint32_t*>(wire + 0x14) = 1;
    *reinterpret_cast<uint64_t*>(wire + 0x50) = 1234567890123456789ull;
    uintptr_t batch[2] = {reinterpret_cast<uintptr_t>(wire), reinterpret_cast<uintptr_t>(wire) + sizeof(wire)};
    sample_all_text(batch, 0x78);
    auto result = std::make_unique<ReceivedMessage>();
    poll_received(result.get());
    const bool expected = !invalid && text.size() < sizeof(ReceivedMessage::image_xml);
    CHECK(bool(result->present) == expected);
    if (!expected) return true; // bounded, never publishes partial UTF-8 or malformed data
    CHECK(result->type == 1 && result->native_message_id == 1234567890123456789ull);
    CHECK(std::string(result->sender) == sender && std::string(result->receiver) == receiver);
    const bool large = text.size() >= sizeof(result->text);
    CHECK(result->reserved == (large ? 16u : 0u)); // sync events must not acquire local-send flag 8
    CHECK(std::string(large ? result->image_xml : result->text) == text);
    CHECK(!large || result->text[0] == 0);
    poll_received(result.get()); CHECK(!result->present);
    return true;
}

int main() {
    for (size_t size : {0u, 1u, 1023u, 1024u, 9000u, 65536u, 262143u, 262144u})
        if (!check_text(std::string(size, 'x'))) return 1;
    std::string han341, han342, emoji;
    for (int i = 0; i < 341; ++i) han341 += u8"汉";
    han342 = han341 + u8"汉";
    for (int i = 0; i < 3000; ++i) emoji += u8"🙂汉\n";
    if (!check_text(han341) || !check_text(han342) || !check_text(emoji) ||
        !check_text("wxid_synthetic_peer:\n" + han341, true) ||
        !check_text("wxid_synthetic_peer:\n" + emoji, true) ||
        !check_text(std::string(9000, 'x'), false, true) || !check_text("after invalid input")) return 1;
    std::puts("sync text checks: 15 boundary/Unicode/group/invalid cases passed; no WeChat calls");
    return 0;
}
