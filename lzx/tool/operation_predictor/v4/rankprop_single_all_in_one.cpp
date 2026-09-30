// rankprop_single_all_in_one.cpp
//
// Single-file integration of:
//   1) rankprop_single.hpp
//   2) rankprop_model_data.hpp
//   3) rankprop_single.cpp
//
// Runtime model files are still external:
//   model/rankprop_predictor.ts
//   model/rankprop_ranker.ts
//
// Requires LibTorch.

#include <torch/script.h>
#include <torch/torch.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <ctime>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace rankprop_single {

// ============================================================================
// Part 1: Public API (original rankprop_single.hpp)
// ============================================================================

struct RankPropResult {
    std::string app;
    std::size_t eviction_rank = 0;
    float eviction_score = 0.0f;
    float base_score = 0.0f;
    float delta = 0.0f;
    float rho = 0.0f;
    float expected_distance = 0.0f;
    int distinct_age = 0;
    bool eligible = false;
    float d_hat_fin = 0.0f;
    float p_nr = 0.0f;
};

class RankProp {
public:
    explicit RankProp(const std::string& model_dir, const std::string& device = "cpu");
    ~RankProp();

    RankProp(RankProp&&) noexcept;
    RankProp& operator=(RankProp&&) noexcept;

    RankProp(const RankProp&) = delete;
    RankProp& operator=(const RankProp&) = delete;

    // Call once for every real APP_SWITCH.
    // The returned vector is in eviction order:
    // result[0] is the highest-priority eviction candidate.
    std::vector<RankPropResult> operator()(
        const std::string& current_app,
        const std::vector<std::string>& opened_apps,
        double timestamp,
        float pressure = 0.0f,
        float reclaim_target = 0.0f);

    // Convenience interface: return only app names in eviction order.
    std::vector<std::string> predict(
        const std::string& current_app,
        const std::vector<std::string>& opened_apps,
        double timestamp,
        float pressure = 0.0f,
        float reclaim_target = 0.0f);

    // Clear online history/state. Loaded TorchScript models are retained.
    void reset();

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

// ============================================================================
// Part 2: Static model metadata (original rankprop_model_data.hpp)
// ============================================================================

namespace model_data {

inline constexpr int kDClip = 16;
inline constexpr double kTauAlpha = 10;
inline constexpr int kContextLen = 32;
inline constexpr int kMaxCandidates = 30;
inline constexpr int kMinPriorLaunches = 5;
inline constexpr int kPadId = 30;
inline constexpr int kUnknownId = 31;

inline constexpr std::array<double, 17> kFallback = {
    2.2028586864471436,
    2.2028586864471436,
    3.879014253616333,
    5.1150288581848145,
    6.1883420944213867,
    7.1914224624633789,
    8.107762336730957,
    9.0026254653930664,
    9.8745288848876953,
    10.720720291137695,
    11.511977195739746,
    12.305873870849609,
    13.107514381408691,
    13.897029876708984,
    14.595070838928223,
    15.29007625579834,
    16
};

inline constexpr std::array<std::pair<std::string_view, int>, 32> kVocab = {{
    std::pair<std::string_view, int>{"Firefox", 0},
    std::pair<std::string_view, int>{"LibreOffice", 1},
    std::pair<std::string_view, int>{"VLC", 2},
    std::pair<std::string_view, int>{"GIMP", 3},
    std::pair<std::string_view, int>{"Audacity", 4},
    std::pair<std::string_view, int>{"Thunderbird", 5},
    std::pair<std::string_view, int>{"Evince", 6},
    std::pair<std::string_view, int>{"Files", 7},
    std::pair<std::string_view, int>{"Calculator", 8},
    std::pair<std::string_view, int>{"Calendar", 9},
    std::pair<std::string_view, int>{"Rhythmbox", 10},
    std::pair<std::string_view, int>{"ImageViewer", 11},
    std::pair<std::string_view, int>{"Shotwell", 12},
    std::pair<std::string_view, int>{"SystemMonitor", 13},
    std::pair<std::string_view, int>{"Solitaire", 14},
    std::pair<std::string_view, int>{"Falkon", 15},
    std::pair<std::string_view, int>{"Konqueror", 16},
    std::pair<std::string_view, int>{"Pidgin", 17},
    std::pair<std::string_view, int>{"Gajim", 18},
    std::pair<std::string_view, int>{"Dino", 19},
    std::pair<std::string_view, int>{"PsiPlus", 20},
    std::pair<std::string_view, int>{"Kaidan", 21},
    std::pair<std::string_view, int>{"GNOMESoftware", 22},
    std::pair<std::string_view, int>{"Evolution", 23},
    std::pair<std::string_view, int>{"ClawsMail", 24},
    std::pair<std::string_view, int>{"GNOMEClocks", 25},
    std::pair<std::string_view, int>{"GNOMEContacts", 26},
    std::pair<std::string_view, int>{"Marble", 27},
    std::pair<std::string_view, int>{"GNOMEMines", 28},
    std::pair<std::string_view, int>{"GNOMEControlCenter", 29},
    std::pair<std::string_view, int>{"<PAD>", 30},
    std::pair<std::string_view, int>{"<UNKNOWN>", 31}
}};

} // namespace model_data

// ============================================================================
// Part 3: Implementation (original rankprop_single.cpp)
// ============================================================================

namespace {

struct Prediction {
    float d_hat_fin = 0.0f;
    float p_nr = 0.0f;
    bool eligible = false;
    int evidence_count = 0;
};

struct AppState {
    Prediction prediction;
    int distinct_age = 0;
    std::unordered_set<std::string> interveners;
    int launch_count = 0;
    std::int64_t last_launch_seq = -1;
};

float clip01(float x) {
    return std::max(0.0f, std::min(1.0f, x));
}

int token_id(const std::string& app) {
    for (const auto& kv : model_data::kVocab) {
        if (kv.first == app) {
            return kv.second;
        }
    }
    return model_data::kUnknownId;
}

float fallback_score(int age) {
    const int k = std::max(0, std::min(model_data::kDClip, age));
    return static_cast<float>(
        model_data::kFallback[static_cast<std::size_t>(k)]
    );
}

float alpha_from_count(int n) {
    n = std::max(0, n);
    const double tau = model_data::kTauAlpha;
    return static_cast<float>((n || tau) ? n / (n + tau) : 0.0);
}

float model_future_score(const Prediction& p) {
    const float dc = static_cast<float>(model_data::kDClip);
    const float dnr = dc + 1.0f;

    const float d = std::max(0.0f, std::min(dc, p.d_hat_fin));
    const float nr = clip01(p.p_nr);

    return (1.0f - nr) * d + nr * dnr;
}

struct Calibrated {
    float rho;
    float alpha;
    float fallback;
};

Calibrated calibrated_score(const Prediction& p, int age) {
    const float b = fallback_score(age);
    const float a = alpha_from_count(p.evidence_count);
    const float m = model_future_score(p);
    const float xi = p.eligible ? 1.0f : 0.0f;

    return {
        b + xi * a * (m - b),
        a,
        b
    };
}

float time_of_day(double timestamp) {
    std::time_t t = static_cast<std::time_t>(timestamp);
    std::tm tmv{};

#if defined(_WIN32)
    localtime_s(&tmv, &t);
#else
    localtime_r(&t, &tmv);
#endif

    return static_cast<float>(
        (tmv.tm_hour * 3600 + tmv.tm_min * 60 + tmv.tm_sec) / 86400.0
    );
}

} // anonymous namespace

struct RankProp::Impl {
    torch::jit::script::Module predictor;
    torch::jit::script::Module ranker;

    torch::Device device{torch::kCPU};

    std::unordered_map<std::string, AppState> states;

    std::vector<std::string> history_apps;
    std::vector<double> history_ts;

    std::int64_t seq = 0;
    double last_timestamp = -std::numeric_limits<double>::infinity();

    Impl(const std::string& model_dir, const std::string& device_name) {
        if (device_name == "cpu") {
            device = torch::Device(torch::kCPU);
        } else if (device_name == "cuda") {
            if (!torch::cuda::is_available()) {
                throw std::runtime_error("RankProp: CUDA unavailable");
            }
            device = torch::Device(torch::kCUDA);
        } else {
            throw std::invalid_argument(
                "RankProp: device must be cpu or cuda"
            );
        }

        predictor = torch::jit::load(
            model_dir + "/rankprop_predictor.ts",
            device
        );

        ranker = torch::jit::load(
            model_dir + "/rankprop_ranker.ts",
            device
        );

        predictor.eval();
        ranker.eval();
    }

    Prediction predict_relaunch(
        const std::string& app,
        int launch_count,
        double timestamp) {

        auto apps = history_apps;
        auto ts = history_ts;

        apps.push_back(app);
        ts.push_back(timestamp);

        while (apps.size() >
               static_cast<std::size_t>(model_data::kContextLen)) {
            apps.erase(apps.begin());
            ts.erase(ts.begin());
        }

        const int valid = static_cast<int>(apps.size());
        const int pad = model_data::kContextLen - valid;

        std::vector<std::int64_t> tv(
            model_data::kContextLen,
            model_data::kPadId
        );

        std::vector<float> nv(
            model_data::kContextLen * 3,
            0.0f
        );

        std::vector<std::uint8_t> mv(
            model_data::kContextLen,
            0
        );

        const double denom =
            std::log1p(7.0 * 24.0 * 3600.0);

        for (int i = 0; i < valid; ++i) {
            const int dst = pad + i;

            tv[dst] = token_id(apps[i]);

            float gap = 0.0f;

            if (i > 0) {
                const double g =
                    std::max(0.0, ts[i] - ts[i - 1]);

                gap = static_cast<float>(
                    std::min(
                        1.0,
                        std::log1p(g) / denom
                    )
                );
            }

            const float evidence =
                (i == valid - 1 && launch_count)
                    ? static_cast<float>(
                        launch_count /
                        (launch_count + 10.0)
                    )
                    : 0.0f;

            nv[dst * 3] = time_of_day(ts[i]);
            nv[dst * 3 + 1] = gap;
            nv[dst * 3 + 2] = evidence;

            mv[dst] = 1;
        }

        auto tokens = torch::from_blob(
            tv.data(),
            {1, model_data::kContextLen},
            torch::TensorOptions().dtype(torch::kLong)
        ).clone().to(device);

        auto numeric = torch::from_blob(
            nv.data(),
            {1, model_data::kContextLen, 3},
            torch::TensorOptions().dtype(torch::kFloat32)
        ).clone().to(device);

        auto mask = torch::from_blob(
            mv.data(),
            {1, model_data::kContextLen},
            torch::TensorOptions().dtype(torch::kUInt8)
        ).to(torch::kBool).clone().to(device);

        torch::NoGradGuard ng;

        auto tup =
            predictor.forward({tokens, numeric, mask})
                     .toTuple();

        const float d =
            tup->elements()[0]
               .toTensor()
               .item<float>();

        const float logit =
            tup->elements()[1]
               .toTensor()
               .item<float>();

        const float pnr =
            1.0f / (1.0f + std::exp(-logit));

        const bool eligible =
            token_id(app) != model_data::kUnknownId &&
            launch_count >= model_data::kMinPriorLaunches;

        return {
            d,
            pnr,
            eligible,
            launch_count
        };
    }

    void reset() {
        states.clear();
        history_apps.clear();
        history_ts.clear();

        seq = 0;

        last_timestamp =
            -std::numeric_limits<double>::infinity();
    }
};

// ============================================================================
// RankProp public methods
// ============================================================================

RankProp::RankProp(
    const std::string& model_dir,
    const std::string& device)
    : impl_(
        std::make_unique<Impl>(
            model_dir,
            device
        )
      ) {
}

RankProp::~RankProp() = default;

RankProp::RankProp(RankProp&&) noexcept = default;

RankProp&
RankProp::operator=(RankProp&&) noexcept = default;

void RankProp::reset() {
    impl_->reset();
}

std::vector<RankPropResult>
RankProp::operator()(
    const std::string& current_app,
    const std::vector<std::string>& opened_apps,
    double timestamp,
    float pressure,
    float reclaim_target) {

    if (current_app.empty()) {
        throw std::invalid_argument(
            "RankProp: current_app empty"
        );
    }

    if (!std::isfinite(timestamp)) {
        throw std::invalid_argument(
            "RankProp: invalid timestamp"
        );
    }

    if (timestamp < impl_->last_timestamp) {
        throw std::invalid_argument(
            "RankProp: timestamp must be nondecreasing"
        );
    }

    impl_->last_timestamp = timestamp;

    pressure = clip01(pressure);
    reclaim_target = clip01(reclaim_target);

    // ------------------------------------------------------------------------
    // Build resident-app set and validate opened_apps.
    // current_app is always considered resident.
    // ------------------------------------------------------------------------

    std::unordered_set<std::string> resident;

    for (const auto& app : opened_apps) {
        if (app.empty()) {
            throw std::invalid_argument(
                "RankProp: empty opened app"
            );
        }

        if (!resident.insert(app).second) {
            throw std::invalid_argument(
                "RankProp: duplicate opened app: " + app
            );
        }

        impl_->states.try_emplace(app);
    }

    resident.insert(current_app);
    impl_->states.try_emplace(current_app);

    // ------------------------------------------------------------------------
    // Update distinct-age state for every resident background app.
    // ------------------------------------------------------------------------

    for (const auto& app : resident) {
        if (app == current_app) {
            continue;
        }

        auto& s = impl_->states[app];

        if (s.interveners.insert(current_app).second) {
            ++s.distinct_age;
        }
    }

    // ------------------------------------------------------------------------
    // Predict relaunch behavior for the newly launched/current app.
    // ------------------------------------------------------------------------

    auto& cur = impl_->states[current_app];

    cur.prediction = impl_->predict_relaunch(
        current_app,
        cur.launch_count,
        timestamp
    );

    cur.distinct_age = 0;
    cur.interveners.clear();
    cur.last_launch_seq = impl_->seq++;

    ++cur.launch_count;

    // ------------------------------------------------------------------------
    // Append this APP_SWITCH to the online context history.
    // ------------------------------------------------------------------------

    impl_->history_apps.push_back(current_app);
    impl_->history_ts.push_back(timestamp);

    while (impl_->history_apps.size() >
           static_cast<std::size_t>(model_data::kContextLen)) {

        impl_->history_apps.erase(
            impl_->history_apps.begin()
        );

        impl_->history_ts.erase(
            impl_->history_ts.begin()
        );
    }

    // ------------------------------------------------------------------------
    // Candidate eviction apps = all resident apps except current foreground app.
    // Sort candidates into LRU order using last_launch_seq.
    // ------------------------------------------------------------------------

    std::vector<std::string> candidates;

    for (const auto& app : resident) {
        if (app != current_app) {
            candidates.push_back(app);
        }
    }

    std::sort(
        candidates.begin(),
        candidates.end(),
        [&](const auto& a, const auto& b) {
            const auto& sa = impl_->states[a];
            const auto& sb = impl_->states[b];

            return sa.last_launch_seq != sb.last_launch_seq
                ? sa.last_launch_seq < sb.last_launch_seq
                : a < b;
        }
    );

    if (candidates.empty()) {
        return {};
    }

    if (candidates.size() >
        static_cast<std::size_t>(
            model_data::kMaxCandidates
        )) {

        throw std::invalid_argument(
            "RankProp: too many candidates"
        );
    }

    // ------------------------------------------------------------------------
    // Build 9-D per-candidate features for the learned RankProp ranker.
    // ------------------------------------------------------------------------

    const std::size_t n = candidates.size();

    const float dc =
        static_cast<float>(model_data::kDClip);

    const float dnr = dc + 1.0f;

    std::vector<float> rows(n * 9);
    std::vector<float> rhos(n);

    for (std::size_t i = 0; i < n; ++i) {
        const auto& s =
            impl_->states[candidates[i]];

        const auto c =
            calibrated_score(
                s.prediction,
                s.distinct_age
            );

        const float m =
            model_future_score(s.prediction);

        const float rho =
            std::min(dc, c.rho);

        // candidates are already in LRU order.
        // Oldest candidate receives the largest LRU feature.
        const float lru =
            n <= 1
                ? 1.0f
                : 1.0f -
                    static_cast<float>(i) /
                    static_cast<float>(n - 1);

        rhos[i] = rho;

        float* r = &rows[i * 9];

        r[0] = clip01(rho / dnr);
        r[1] = clip01(m / dnr);
        r[2] = clip01(
            s.prediction.d_hat_fin / dc
        );
        r[3] = clip01(
            s.prediction.p_nr
        );
        r[4] = clip01(
            c.alpha
        );
        r[5] = clip01(
            static_cast<float>(
                std::min(
                    s.distinct_age,
                    model_data::kDClip
                )
            ) / dc
        );
        r[6] = clip01(
            c.fallback / dc
        );
        r[7] =
            s.prediction.eligible
                ? 1.0f
                : 0.0f;
        r[8] = lru;
    }

    // ------------------------------------------------------------------------
    // Run TorchScript RankProp ranker.
    // ------------------------------------------------------------------------

    auto x = torch::from_blob(
        rows.data(),
        {
            1,
            static_cast<long>(n),
            9
        },
        torch::TensorOptions()
            .dtype(torch::kFloat32)
    ).clone().to(impl_->device);

    auto mask = torch::ones(
        {
            1,
            static_cast<long>(n)
        },
        torch::TensorOptions()
            .dtype(torch::kBool)
            .device(impl_->device)
    );

    auto p = torch::tensor(
        {pressure},
        torch::TensorOptions()
            .dtype(torch::kFloat32)
            .device(impl_->device)
    );

    auto t = torch::tensor(
        {reclaim_target},
        torch::TensorOptions()
            .dtype(torch::kFloat32)
            .device(impl_->device)
    );

    torch::NoGradGuard ng;

    auto tup =
        impl_->ranker
            .forward({x, mask, p, t})
            .toTuple();

    auto st =
        tup->elements()[0]
            .toTensor()
            .cpu()
            .contiguous();

    auto dt =
        tup->elements()[1]
            .toTensor()
            .cpu()
            .contiguous();

    const float* scores =
        st.data_ptr<float>();

    const float* deltas =
        dt.data_ptr<float>();

    // ------------------------------------------------------------------------
    // Higher eviction score = higher eviction priority.
    // ------------------------------------------------------------------------

    std::vector<std::size_t> order(n);

    for (std::size_t i = 0; i < n; ++i) {
        order[i] = i;
    }

    std::stable_sort(
        order.begin(),
        order.end(),
        [&](auto a, auto b) {
            return scores[a] > scores[b];
        }
    );

    // ------------------------------------------------------------------------
    // Build detailed output.
    // ------------------------------------------------------------------------

    std::vector<RankPropResult> out;
    out.reserve(n);

    for (std::size_t rank = 0; rank < n; ++rank) {
        const auto i = order[rank];

        const auto& s =
            impl_->states[candidates[i]];

        out.push_back({
            candidates[i],               // app
            rank,                        // eviction_rank
            scores[i],                   // eviction_score
            rows[i * 9],                 // base_score
            deltas[i],                   // delta
            rhos[i],                     // rho
            rhos[i],                     // expected_distance
            s.distinct_age,              // distinct_age
            s.prediction.eligible,       // eligible
            s.prediction.d_hat_fin,      // d_hat_fin
            s.prediction.p_nr            // p_nr
        });
    }

    return out;
}

std::vector<std::string>
RankProp::predict(
    const std::string& current_app,
    const std::vector<std::string>& opened_apps,
    double timestamp,
    float pressure,
    float reclaim_target) {

    auto detailed =
        (*this)(
            current_app,
            opened_apps,
            timestamp,
            pressure,
            reclaim_target
        );

    std::vector<std::string> out;
    out.reserve(detailed.size());

    for (const auto& x : detailed) {
        out.push_back(x.app);
    }

    return out;
}

} // namespace rankprop_single

#include <chrono>
#include <cctype>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <thread>

namespace online_monitor {

constexpr std::size_t kEvaluationHorizon = 20;

std::string lower_copy(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string trim(std::string s) {
    while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back()))) s.pop_back();
    std::size_t p = 0;
    while (p < s.size() && std::isspace(static_cast<unsigned char>(s[p]))) ++p;
    return s.substr(p);
}

std::string run_command(const std::string& cmd) {
    std::array<char, 512> buf{};
    std::string out;
    FILE* pipe = popen(cmd.c_str(), "r");
    if (!pipe) return {};
    while (fgets(buf.data(), static_cast<int>(buf.size()), pipe)) out += buf.data();
    pclose(pipe);
    return trim(out);
}

std::string read_file(const std::filesystem::path& path, bool binary = false) {
    std::ifstream in(path, binary ? std::ios::binary : std::ios::in);
    if (!in) return {};
    std::ostringstream ss;
    ss << in.rdbuf();
    std::string s = ss.str();
    if (binary) std::replace(s.begin(), s.end(), '\0', ' ');
    return trim(s);
}

// 把 Linux 进程/命令行/窗口信息映射为训练模型中的 App 名称。
// 如果你以后增加自己的训练 App，主要扩展这里即可。
std::optional<std::string> normalize_app(const std::string& raw) {
    const std::string s = lower_copy(raw);
    auto has = [&](std::string_view x) { return s.find(x) != std::string::npos; };

    if (has("firefox")) return "Firefox";
    if (has("libreoffice") || has("soffice")) return "LibreOffice";
    if (has("vlc")) return "VLC";
    if (has("gimp")) return "GIMP";
    if (has("audacity")) return "Audacity";
    if (has("thunderbird")) return "Thunderbird";
    if (has("evince")) return "Evince";
    if (has("nautilus")) return "Files";
    if (has("gnome-calculator") || has("calculator")) return "Calculator";
    if (has("gnome-calendar") || has("calendar")) return "Calendar";
    if (has("rhythmbox")) return "Rhythmbox";
    if (has("eog") || has("image viewer") || has("imageviewer")) return "ImageViewer";
    if (has("shotwell")) return "Shotwell";
    if (has("gnome-system-monitor") || has("system monitor")) return "SystemMonitor";
    if (has("aisleriot") || has("solitaire")) return "Solitaire";
    if (has("falkon")) return "Falkon";
    if (has("konqueror")) return "Konqueror";
    if (has("pidgin")) return "Pidgin";
    if (has("gajim")) return "Gajim";
    if (has("dino")) return "Dino";
    if (has("psi-plus") || has("psiplus")) return "PsiPlus";
    if (has("kaidan")) return "Kaidan";
    if (has("gnome-software")) return "GNOMESoftware";
    if (has("evolution")) return "Evolution";
    if (has("claws-mail") || has("clawsmail")) return "ClawsMail";
    if (has("gnome-clocks")) return "GNOMEClocks";
    if (has("gnome-contacts")) return "GNOMEContacts";
    if (has("marble")) return "Marble";
    if (has("gnome-mines") || has("gnomemines")) return "GNOMEMines";
    if (has("gnome-control-center") || has("settings")) return "GNOMEControlCenter";
    return std::nullopt;
}

std::optional<std::string> foreground_app() {
    const std::string pid = run_command("xdotool getactivewindow getwindowpid 2>/dev/null");
    if (pid.empty()) return std::nullopt;

    const std::filesystem::path base = std::filesystem::path("/proc") / pid;
    const std::string comm = read_file(base / "comm");
    const std::string cmdline = read_file(base / "cmdline", true);
    const std::string window = run_command("xdotool getactivewindow getwindowname 2>/dev/null");
    return normalize_app(comm + " " + cmdline + " " + window);
}

std::vector<std::string> resident_model_apps() {
    std::unordered_set<std::string> found;
    std::error_code ec;

    for (const auto& entry : std::filesystem::directory_iterator("/proc", ec)) {
        if (ec) break;
        const std::string name = entry.path().filename().string();
        if (name.empty() || !std::all_of(name.begin(), name.end(), ::isdigit)) continue;

        const std::string comm = read_file(entry.path() / "comm");
        const std::string cmdline = read_file(entry.path() / "cmdline", true);
        if (auto app = normalize_app(comm + " " + cmdline)) found.insert(*app);
    }

    std::vector<std::string> out(found.begin(), found.end());
    std::sort(out.begin(), out.end());
    return out;
}

struct PendingEvaluation {
    std::size_t id = 0;
    std::string predicted_top1;
    std::vector<std::string> predicted_top3;
    std::vector<std::string> candidates;
    std::unordered_map<std::string, std::size_t> first_future_use;
    std::size_t future_steps = 0;
};

struct AccuracyTracker {
    std::deque<PendingEvaluation> pending;
    std::size_t next_id = 1;
    std::size_t evaluated = 0;
    std::size_t top1_hits = 0;
    std::size_t top3_hits = 0;

    void observe_switch(const std::string& current_app) {
        for (auto& e : pending) {
            ++e.future_steps;
            if (std::find(e.candidates.begin(), e.candidates.end(), current_app) != e.candidates.end() &&
                !e.first_future_use.count(current_app)) {
                e.first_future_use[current_app] = e.future_steps;
            }
        }
        finalize_ready();
    }

    void add_prediction(const std::vector<rankprop_single::RankPropResult>& result) {
        if (result.empty()) return;
        PendingEvaluation e;
        e.id = next_id++;
        e.predicted_top1 = result.front().app;
        for (std::size_t i = 0; i < result.size(); ++i) {
            e.candidates.push_back(result[i].app);
            if (i < 3) e.predicted_top3.push_back(result[i].app);
        }
        pending.push_back(std::move(e));
    }

    void finalize_ready() {
        while (!pending.empty() && pending.front().future_steps >= kEvaluationHorizon) {
            PendingEvaluation e = std::move(pending.front());
            pending.pop_front();

            // 在 H 次未来切换内没有再次出现的候选，其 future distance 记为 H+1。
            // 因此可能有多个并列的 Belady/MIN 最优 victim。
            std::size_t max_distance = 0;
            std::vector<std::string> optimal_victims;
            for (const auto& app : e.candidates) {
                const auto it = e.first_future_use.find(app);
                const std::size_t d = (it == e.first_future_use.end())
                                        ? kEvaluationHorizon + 1
                                        : it->second;
                if (d > max_distance) {
                    max_distance = d;
                    optimal_victims.assign(1, app);
                } else if (d == max_distance) {
                    optimal_victims.push_back(app);
                }
            }

            const bool top1_hit = std::find(optimal_victims.begin(), optimal_victims.end(),
                                            e.predicted_top1) != optimal_victims.end();
            bool top3_hit = false;
            for (const auto& app : e.predicted_top3) {
                if (std::find(optimal_victims.begin(), optimal_victims.end(), app) != optimal_victims.end()) {
                    top3_hit = true;
                    break;
                }
            }

            ++evaluated;
            if (top1_hit) ++top1_hits;
            if (top3_hit) ++top3_hits;

            std::cout << "\n[Delayed evaluation #" << e.id << "]\n"
                      << "Predicted Top-1 : " << e.predicted_top1 << "\n"
                      << "Optimal victim(s): ";
            for (std::size_t i = 0; i < optimal_victims.size(); ++i) {
                if (i) std::cout << ", ";
                std::cout << optimal_victims[i];
            }
            std::cout << "\nTop-1 result    : " << (top1_hit ? "CORRECT" : "WRONG")
                      << "\nTop-3 result    : " << (top3_hit ? "HIT" : "MISS") << "\n";
            print_summary();
        }
    }

    void print_summary() const {
        const double top1 = evaluated ? 100.0 * static_cast<double>(top1_hits) / evaluated : 0.0;
        const double top3 = evaluated ? 100.0 * static_cast<double>(top3_hits) / evaluated : 0.0;
        std::cout << "Accuracy (H=" << kEvaluationHorizon << ")"
                  << "  Top-1=" << top1 << "% (" << top1_hits << "/" << evaluated << ")"
                  << "  Top-3=" << top3 << "% (" << top3_hits << "/" << evaluated << ")"
                  << "  Pending=" << pending.size() << "\n";
    }
};

void print_apps(const std::vector<std::string>& apps) {
    for (std::size_t i = 0; i < apps.size(); ++i) {
        if (i) std::cout << ", ";
        std::cout << apps[i];
    }
    std::cout << '\n';
}

} // namespace online_monitor

int main(int argc, char** argv) {
    try {
        const std::string model_dir = (argc >= 2) ? argv[1] : "./model";
        rankprop_single::RankProp rankprop(model_dir, "cpu");
        online_monitor::AccuracyTracker accuracy;

        std::cout << "============================================================\n"
                  << " RankProp automatic online inference + delayed accuracy\n"
                  << "============================================================\n"
                  << "Model dir              : " << model_dir << "\n"
                  << "Foreground source       : X11 / xdotool\n"
                  << "Polling interval        : 250 ms\n"
                  << "Accuracy horizon        : " << online_monitor::kEvaluationHorizon << " APP_SWITCH events\n"
                  << "Accuracy ground truth   : farthest next use (Belady/MIN); no-use-in-window = H+1\n"
                  << "Press Ctrl+C to stop.\n\n";

        std::string previous_app;

        while (true) {
            const auto current = online_monitor::foreground_app();

            if (current && *current != previous_app) {
                // 先用这次真实 APP_SWITCH 更新以前预测的未来轨迹，避免数据泄漏。
                accuracy.observe_switch(*current);

                auto resident = online_monitor::resident_model_apps();
                if (std::find(resident.begin(), resident.end(), *current) == resident.end())
                    resident.push_back(*current);
                std::sort(resident.begin(), resident.end());
                resident.erase(std::unique(resident.begin(), resident.end()), resident.end());

                const auto now = std::chrono::system_clock::now();
                const double timestamp = std::chrono::duration<double>(now.time_since_epoch()).count();

                auto result = rankprop(*current, resident, timestamp);

                std::cout << "\n============================================================\n"
                          << "APP_SWITCH -> " << *current << "\n"
                          << "Resident model apps: ";
                online_monitor::print_apps(resident);
                std::cout << "------------------------------------------------------------\n";

                if (result.empty()) {
                    std::cout << "No background eviction candidates.\n";
                } else {
                    for (const auto& item : result) {
                        std::cout << "rank=" << item.eviction_rank
                                  << " app=" << item.app
                                  << " score=" << item.eviction_score
                                  << " rho=" << item.rho
                                  << " age=" << item.distinct_age
                                  << " eligible=" << item.eligible
                                  << " d_hat=" << item.d_hat_fin
                                  << " p_nr=" << item.p_nr << '\n';
                    }

                    std::cout << ">>> Highest-priority eviction candidate: "
                              << result.front().app << '\n';

                    // 当前预测只能在未来 H 次 APP_SWITCH 之后被评价。
                    accuracy.add_prediction(result);
                    accuracy.print_summary();
                }

                previous_app = *current;
            }

            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }

    } catch (const c10::Error& e) {
        std::cerr << "LibTorch error: " << e.what() << '\n';
        return 1;
    } catch (const std::exception& e) {
        std::cerr << "RankProp error: " << e.what() << '\n';
        return 1;
    }
}

