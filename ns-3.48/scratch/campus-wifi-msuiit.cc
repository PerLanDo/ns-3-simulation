/*
 * MSU-IIT campus Wi-Fi evaluation model
 * SPDX-License-Identifier: GPL-2.0-only
 *
 * Undergraduate thesis instrument for evaluating and enhancing a high-density campus Wi-Fi
 * framework. Implements the three-scenario comparative framework:
 *
 *   1. baseline  - single AP, 802.11n, 2.4 GHz, 20 MHz. High contention.
 *   2. rftuning  - dual-band AP, 2.4 GHz 20 MHz + 5 GHz 80 MHz, most clients offloaded to 5 GHz.
 *   3. ax        - two 802.11ax APs on non-overlapping 5 GHz 80 MHz channels, DL/UL OFDMA,
 *                  clients load balanced across them.
 *
 * All three share the same client population, traffic mix and propagation environment, so any
 * difference in the reported QoS comes from the RF/MAC strategy alone.
 *
 * Metrics are collected with FlowMonitor and written as:
 *   <outDir>/<tag>-flowmon.xml   full FlowMonitor record
 *   <outDir>/<tag>-flows.csv     one row per flow
 *   <outDir>/summary.csv         one appended row per run (input for the dashboard)
 *
 * Examples:
 *   ./ns3 run "campus-wifi-msuiit --scenario=baseline --nSta=10 --simTime=10s"
 *   ./ns3 run "campus-wifi-msuiit --scenario=rftuning --nSta=60 --zone=ccs_hub"
 *   ./ns3 run "campus-wifi-msuiit --scenario=ax --nSta=100 --simTime=20s"
 */

#include "ns3/applications-module.h"
#include "ns3/core-module.h"
#include "ns3/flow-monitor-module.h"
#include "ns3/internet-module.h"
#include "ns3/mobility-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/propagation-module.h"
#include "ns3/spectrum-module.h"
#include "ns3/wifi-module.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
#include <sstream>
#include <string>
#include <vector>

using namespace ns3;

NS_LOG_COMPONENT_DEFINE("CampusWifiMsuiit");

namespace
{

/**
 * Physical characteristics of one campus zone. Mirrors data/dummy/aps.csv; every value is a
 * placeholder until the site survey replaces it.
 */
struct ZonePreset
{
    std::string label;
    double radiusM;           //!< how far clients spread from the AP
    double pathLossExponent;  //!< LogDistance exponent for the environment
    double referenceLossDb;   //!< LogDistance loss at 1 m
    double apHeightM;
};

ZonePreset
LookupZone(const std::string& zone)
{
    if (zone == "library")
    {
        return {"Main Library", 25.0, 3.2, 40.0, 3.0};
    }
    if (zone == "ccs_hub")
    {
        return {"CCS Study Hub", 20.0, 3.0, 40.0, 3.0};
    }
    if (zone == "gym")
    {
        return {"University Gymnasium", 40.0, 2.6, 40.0, 6.0};
    }
    if (zone == "lawn")
    {
        return {"Campus Lawn", 60.0, 2.2, 40.0, 4.5};
    }
    if (zone == "college")
    {
        return {"College Building", 22.0, 3.4, 40.0, 3.0};
    }
    NS_ABORT_MSG("Unknown zone '" << zone << "'. Use library, ccs_hub, gym, lawn or college.");
    return {};
}

/** One application class from data/dummy/traffic_mix.csv. */
struct TrafficClass
{
    std::string name;
    uint8_t tos;         //!< maps to a Wi-Fi access category
    uint64_t rateBps;
    uint32_t packetBytes;
};

// Defaults mirror data/dummy/traffic_mix.csv and can be overridden from the command line, so the
// dummy dataset can be edited without recompiling.
TrafficClass BROWSING{"browsing", 0x00, 1500000, 1000}; // AC_BE
TrafficClass VIDEO{"video", 0xb8, 4000000, 1200};       // AC_VI
TrafficClass VOIP{"voip", 0xc0, 64000, 160};            // AC_VO

/**
 * Application port bases. The station index is encoded as an offset from these, which lets the
 * FlowMonitor post-processing recover the traffic class of every flow.
 */
constexpr uint16_t DOWNLINK_BASE_PORT = 5000;
constexpr uint16_t UPLINK_BASE_PORT = 6000;

/** Running QoS totals for one traffic class (or for the whole run). */
struct QosAccumulator
{
    uint64_t txPackets{0};
    uint64_t rxPackets{0};
    uint64_t lostPackets{0};
    uint64_t rxBytes{0};
    double delaySumMs{0.0};
    double jitterSumMs{0.0};
    uint64_t jitterSamples{0};
    uint32_t flows{0};
    uint32_t stations{0};
};

/** One basic service set: an AP radio plus the stations associated to it. */
struct Bss
{
    std::string ssid;
    std::string channelSettings;
    NetDeviceContainer apDevice;
    NetDeviceContainer staDevices;
    std::vector<uint32_t> staIndices; //!< global station indices served by this BSS
    uint32_t apNodeIndex;
};

/**
 * Build one BSS on the shared spectrum channel. Stations are installed before the AP so the
 * multi-user scheduler, which only exists on the AP side, does not leak into the station MACs.
 */
void
BuildBss(Bss& bss,
         Ptr<SpectrumChannel> spectrumChannel,
         WifiStandard standard,
         const std::string& rateManager,
         bool enableOfdma,
         double txPowerDbm,
         Ptr<Node> apNode,
         const NodeContainer& staNodes,
         bool enablePcap,
         const std::string& pcapPrefix)
{
    WifiHelper wifi;
    wifi.SetStandard(standard);

    if (rateManager == "ideal")
    {
        wifi.SetRemoteStationManager("ns3::IdealWifiManager");
    }
    else if (rateManager == "minstrel")
    {
        wifi.SetRemoteStationManager("ns3::MinstrelHtWifiManager");
    }
    else
    {
        NS_ABORT_MSG("Unknown rateManager '" << rateManager << "'. Use ideal or minstrel.");
    }

    if (standard == WIFI_STANDARD_80211ax)
    {
        wifi.ConfigHeOptions("GuardInterval", TimeValue(NanoSeconds(800)));
    }

    SpectrumWifiPhyHelper phy;
    phy.SetChannel(spectrumChannel);
    phy.SetPcapDataLinkType(WifiPhyHelper::DLT_IEEE802_11_RADIO);
    phy.Set("ChannelSettings", StringValue(bss.channelSettings));
    phy.Set("TxPowerStart", DoubleValue(txPowerDbm));
    phy.Set("TxPowerEnd", DoubleValue(txPowerDbm));

    Ssid ssid = Ssid(bss.ssid);
    WifiMacHelper mac;

    mac.SetType("ns3::StaWifiMac",
                "Ssid",
                SsidValue(ssid),
                "MpduBufferSize",
                UintegerValue(enableOfdma ? 256 : 64));
    bss.staDevices = wifi.Install(phy, mac, staNodes);

    if (enableOfdma)
    {
        mac.SetMultiUserScheduler("ns3::RrMultiUserScheduler",
                                  "EnableUlOfdma",
                                  BooleanValue(true),
                                  "EnableBsrp",
                                  BooleanValue(false));
    }
    mac.SetType("ns3::ApWifiMac", "Ssid", SsidValue(ssid), "EnableBeaconJitter", BooleanValue(true));
    bss.apDevice = wifi.Install(phy, mac, NodeContainer(apNode));

    if (enablePcap)
    {
        phy.EnablePcap(pcapPrefix + "-ap", bss.apDevice);
    }
}

/** Build the propagation chain for one BSS. Called once per BSS; see the call site. */
Ptr<SpectrumChannel>
BuildSpectrumChannel(const std::string& propagation,
                     double pathLossExponent,
                     double referenceLossDb)
{
    auto spectrumChannel = CreateObject<MultiModelSpectrumChannel>();
    spectrumChannel->SetPropagationDelayModel(CreateObject<ConstantSpeedPropagationDelayModel>());

    Ptr<PropagationLossModel> baseLoss;
    if (propagation == "friis")
    {
        baseLoss = CreateObject<FriisPropagationLossModel>();
    }
    else if (propagation == "logdistance" || propagation == "nakagami")
    {
        auto logDistance = CreateObject<LogDistancePropagationLossModel>();
        logDistance->SetAttribute("Exponent", DoubleValue(pathLossExponent));
        logDistance->SetAttribute("ReferenceDistance", DoubleValue(1.0));
        logDistance->SetAttribute("ReferenceLoss", DoubleValue(referenceLossDb));
        baseLoss = logDistance;
    }
    else
    {
        NS_ABORT_MSG("Unknown propagation '" << propagation
                                             << "'. Use logdistance, friis or nakagami.");
    }

    // Nakagami layers fast fading on top of the distance-dependent loss rather than replacing it.
    if (propagation == "nakagami")
    {
        baseLoss->SetNext(CreateObject<NakagamiPropagationLossModel>());
    }

    spectrumChannel->AddPropagationLossModel(baseLoss);
    return spectrumChannel;
}

/** Deterministic traffic-class assignment so every scenario sees an identical application mix. */
const TrafficClass&
ClassifyStation(uint32_t index, uint32_t total, double browsingShare, double videoShare)
{
    const double position = (static_cast<double>(index) + 0.5) / static_cast<double>(total);
    if (position < browsingShare)
    {
        return BROWSING;
    }
    if (position < browsingShare + videoShare)
    {
        return VIDEO;
    }
    return VOIP;
}

} // namespace

int
main(int argc, char* argv[])
{
    std::string scenario = "baseline";
    std::string zone = "library";
    std::string rateManager = "ideal";
    std::string propagation = "logdistance";
    std::string direction = "both";
    std::string outDir = "results";
    std::string backhaulRate = "1Gbps";
    std::string backhaulDelay = "1ms";

    uint32_t nSta = 30;
    Time simTime{"20s"};
    double txPowerDbm = 20.0;
    double fiveToTwoRatio = 0.75; // share of clients steered to 5 GHz in the rftuning scenario
    double apSpacingM = 30.0;     // distance between the two APs in the ax scenario
    double uplinkRatio = 0.2;     // uplink offered load as a fraction of the downlink rate
    double browsingShare = 0.60;
    double videoShare = 0.30;
    uint32_t browsingRateKbps = 1500;
    uint32_t videoRateKbps = 4000;
    uint32_t voipRateKbps = 64;
    uint32_t browsingBytes = 1000;
    uint32_t videoBytes = 1200;
    uint32_t voipBytes = 160;
    uint32_t browsingTos = 0x00;
    uint32_t videoTos = 0xb8;
    uint32_t voipTos = 0xc0;
    double radiusOverrideM = 0.0; // 0 keeps the zone preset
    double pathLossExponent = 0.0;
    double referenceLossDb = 0.0;
    uint32_t seed = 1;
    uint32_t run = 1;
    bool useRts = false;
    bool enablePcap = false;
    bool verbose = false;

    CommandLine cmd(__FILE__);
    cmd.AddValue("scenario", "baseline | rftuning | ax", scenario);
    cmd.AddValue("zone", "library | ccs_hub | gym | lawn | college", zone);
    cmd.AddValue("nSta", "Number of client stations", nSta);
    cmd.AddValue("simTime", "Simulated duration, e.g. 20s", simTime);
    cmd.AddValue("direction", "downlink | uplink | both", direction);
    cmd.AddValue("rateManager", "ideal | minstrel", rateManager);
    cmd.AddValue("propagation", "logdistance | friis | nakagami", propagation);
    cmd.AddValue("txPower", "AP and station transmit power in dBm", txPowerDbm);
    cmd.AddValue("fiveToTwoRatio", "Fraction of clients on 5 GHz (rftuning only)", fiveToTwoRatio);
    cmd.AddValue("apSpacing", "Distance in metres between the two APs (ax only)", apSpacingM);
    cmd.AddValue("uplinkRatio", "Uplink load as a fraction of the downlink rate", uplinkRatio);
    cmd.AddValue("browsingShare", "Fraction of clients running web traffic", browsingShare);
    cmd.AddValue("videoShare", "Fraction of clients running video traffic", videoShare);
    cmd.AddValue("browsingRateKbps", "Offered downlink rate per browsing client", browsingRateKbps);
    cmd.AddValue("videoRateKbps", "Offered downlink rate per video client", videoRateKbps);
    cmd.AddValue("voipRateKbps", "Offered downlink rate per VoIP client", voipRateKbps);
    cmd.AddValue("browsingBytes", "Payload bytes for browsing traffic", browsingBytes);
    cmd.AddValue("videoBytes", "Payload bytes for video traffic", videoBytes);
    cmd.AddValue("voipBytes", "Payload bytes for VoIP traffic", voipBytes);
    cmd.AddValue("browsingTos", "IPv4 TOS byte for browsing traffic (AC_BE)", browsingTos);
    cmd.AddValue("videoTos", "IPv4 TOS byte for video traffic (AC_VI)", videoTos);
    cmd.AddValue("voipTos", "IPv4 TOS byte for VoIP traffic (AC_VO)", voipTos);
    cmd.AddValue("radius", "Override the zone client-spread radius in metres", radiusOverrideM);
    cmd.AddValue("pathLossExponent", "Override the zone path-loss exponent", pathLossExponent);
    cmd.AddValue("referenceLoss", "Override the 1 m reference loss in dB", referenceLossDb);
    cmd.AddValue("backhaulRate", "Wired backhaul data rate", backhaulRate);
    cmd.AddValue("backhaulDelay", "Wired backhaul one-way delay", backhaulDelay);
    cmd.AddValue("useRts", "Enable RTS/CTS for every frame", useRts);
    cmd.AddValue("seed", "RNG seed", seed);
    cmd.AddValue("run", "RNG run number; vary this for repeated trials", run);
    cmd.AddValue("outDir", "Directory for CSV and XML output", outDir);
    cmd.AddValue("pcap", "Write per-AP PCAP traces", enablePcap);
    cmd.AddValue("verbose", "Enable Wi-Fi logging", verbose);
    cmd.Parse(argc, argv);

    NS_ABORT_MSG_IF(nSta == 0, "nSta must be at least 1");
    NS_ABORT_MSG_IF(browsingShare + videoShare > 1.0,
                    "browsingShare + videoShare must not exceed 1.0");
    NS_ABORT_MSG_IF(fiveToTwoRatio < 0.0 || fiveToTwoRatio > 1.0,
                    "fiveToTwoRatio must be between 0.0 and 1.0");

    if (verbose)
    {
        LogComponentEnable("CampusWifiMsuiit", LOG_LEVEL_INFO);
    }
    if (useRts)
    {
        Config::SetDefault("ns3::WifiRemoteStationManager::RtsCtsThreshold", StringValue("0"));
    }

    RngSeedManager::SetSeed(seed);
    RngSeedManager::SetRun(run);

    BROWSING.rateBps = browsingRateKbps * 1000ULL;
    BROWSING.packetBytes = browsingBytes;
    BROWSING.tos = static_cast<uint8_t>(browsingTos);
    VIDEO.rateBps = videoRateKbps * 1000ULL;
    VIDEO.packetBytes = videoBytes;
    VIDEO.tos = static_cast<uint8_t>(videoTos);
    VOIP.rateBps = voipRateKbps * 1000ULL;
    VOIP.packetBytes = voipBytes;
    VOIP.tos = static_cast<uint8_t>(voipTos);

    const ZonePreset preset = LookupZone(zone);
    const double radiusM = (radiusOverrideM > 0.0) ? radiusOverrideM : preset.radiusM;
    const double exponent =
        (pathLossExponent > 0.0) ? pathLossExponent : preset.pathLossExponent;
    const double refLoss = (referenceLossDb > 0.0) ? referenceLossDb : preset.referenceLossDb;

    // ---------------------------------------------------------------------
    // Stage 1: nodes
    // ---------------------------------------------------------------------
    const uint32_t nAp = (scenario == "ax") ? 2 : 1;

    NodeContainer apNodes;
    apNodes.Create(nAp);
    NodeContainer staNodes;
    staNodes.Create(nSta);
    NodeContainer coreNode;
    coreNode.Create(1);
    NodeContainer serverNode;
    serverNode.Create(1);

    // ---------------------------------------------------------------------
    // Stage 2: scenario definition - which BSSs exist and who associates where
    // ---------------------------------------------------------------------
    std::vector<Bss> bssList;
    std::vector<WifiStandard> bssStandard;
    std::vector<bool> bssOfdma;

    if (scenario == "baseline")
    {
        Bss bss;
        bss.ssid = "MSU-IIT-Campus-WiFi";
        bss.channelSettings = "{1, 20, BAND_2_4GHZ, 0}";
        bss.apNodeIndex = 0;
        for (uint32_t i = 0; i < nSta; ++i)
        {
            bss.staIndices.push_back(i);
        }
        bssList.push_back(bss);
        bssStandard.push_back(WIFI_STANDARD_80211n);
        bssOfdma.push_back(false);
    }
    else if (scenario == "rftuning")
    {
        const uint32_t nFive = static_cast<uint32_t>(std::lround(nSta * fiveToTwoRatio));

        Bss bss24;
        bss24.ssid = "MSU-IIT-Campus-WiFi";
        bss24.channelSettings = "{1, 20, BAND_2_4GHZ, 0}";
        bss24.apNodeIndex = 0;

        Bss bss5;
        bss5.ssid = "MSU-IIT-Campus-WiFi-5G";
        bss5.channelSettings = "{42, 80, BAND_5GHZ, 0}";
        bss5.apNodeIndex = 0;

        // Spread the 5 GHz selection evenly across the index range instead of taking a contiguous
        // block, otherwise one band would receive all of the voice clients and skew the comparison.
        for (uint32_t i = 0; i < nSta; ++i)
        {
            const auto before = static_cast<uint32_t>(std::lround(i * fiveToTwoRatio));
            const auto after = static_cast<uint32_t>(std::lround((i + 1) * fiveToTwoRatio));
            if (after > before && bss5.staIndices.size() < nFive)
            {
                bss5.staIndices.push_back(i);
            }
            else
            {
                bss24.staIndices.push_back(i);
            }
        }

        bssList.push_back(bss24);
        bssStandard.push_back(WIFI_STANDARD_80211n);
        bssOfdma.push_back(false);

        bssList.push_back(bss5);
        bssStandard.push_back(WIFI_STANDARD_80211ac);
        bssOfdma.push_back(false);
    }
    else if (scenario == "ax")
    {
        Bss bssA;
        bssA.ssid = "MSU-IIT-Campus-WiFi-AX-1";
        bssA.channelSettings = "{42, 80, BAND_5GHZ, 0}";
        bssA.apNodeIndex = 0;

        Bss bssB;
        bssB.ssid = "MSU-IIT-Campus-WiFi-AX-2";
        bssB.channelSettings = "{106, 80, BAND_5GHZ, 0}";
        bssB.apNodeIndex = 1;

        // Round-robin assignment stands in for controller-driven load balancing.
        for (uint32_t i = 0; i < nSta; ++i)
        {
            (i % 2 == 0 ? bssA : bssB).staIndices.push_back(i);
        }

        bssList.push_back(bssA);
        bssStandard.push_back(WIFI_STANDARD_80211ax);
        bssOfdma.push_back(true);

        bssList.push_back(bssB);
        bssStandard.push_back(WIFI_STANDARD_80211ax);
        bssOfdma.push_back(true);
    }
    else
    {
        NS_ABORT_MSG("Unknown scenario '" << scenario << "'. Use baseline, rftuning or ax.");
    }

    // ---------------------------------------------------------------------
    // Stage 3: mobility and propagation
    // ---------------------------------------------------------------------
    std::vector<Vector> apPositions;
    for (uint32_t a = 0; a < nAp; ++a)
    {
        apPositions.emplace_back(a * apSpacingM, 0.0, preset.apHeightM);
    }

    MobilityHelper apMobility;
    auto apAlloc = CreateObject<ListPositionAllocator>();
    for (const auto& p : apPositions)
    {
        apAlloc->Add(p);
    }
    apMobility.SetPositionAllocator(apAlloc);
    apMobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    apMobility.Install(apNodes);

    // Wired infrastructure sits well away from the radio environment; its position is irrelevant.
    MobilityHelper wiredMobility;
    auto wiredAlloc = CreateObject<ListPositionAllocator>();
    wiredAlloc->Add(Vector(0.0, -100.0, 0.0));
    wiredAlloc->Add(Vector(0.0, -200.0, 0.0));
    wiredMobility.SetPositionAllocator(wiredAlloc);
    wiredMobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
    wiredMobility.Install(coreNode);
    wiredMobility.Install(serverNode);

    // Stations are scattered uniformly in a disc around the AP they associate with.
    for (const auto& bss : bssList)
    {
        const Vector& centre = apPositions[bss.apNodeIndex];
        auto discAlloc = CreateObject<UniformDiscPositionAllocator>();
        discAlloc->SetAttribute("rho", DoubleValue(radiusM));
        discAlloc->SetAttribute("X", DoubleValue(centre.x));
        discAlloc->SetAttribute("Y", DoubleValue(centre.y));
        discAlloc->SetAttribute("Z", DoubleValue(1.5));

        MobilityHelper staMobility;
        staMobility.SetPositionAllocator(discAlloc);
        staMobility.SetMobilityModel("ns3::ConstantPositionMobilityModel");
        for (const uint32_t idx : bss.staIndices)
        {
            staMobility.Install(staNodes.Get(idx));
        }
    }

    // ---------------------------------------------------------------------
    // Stage 4: Wi-Fi devices
    // ---------------------------------------------------------------------
    std::ostringstream tagStream;
    tagStream << scenario << "-" << zone << "-n" << nSta << "-run" << run;
    const std::string tag = tagStream.str();

    std::error_code ec;
    std::filesystem::create_directories(outDir, ec);

    for (size_t b = 0; b < bssList.size(); ++b)
    {
        NodeContainer members;
        for (const uint32_t idx : bssList[b].staIndices)
        {
            members.Add(staNodes.Get(idx));
        }
        if (members.GetN() == 0)
        {
            continue;
        }
        // Each BSS gets its own channel object. A single shared channel would put every
        // BSS in one broadcast domain, and Ipv4GlobalRoutingHelper aborts when it finds
        // two subnets there ("network number confusion"). Separate objects are also the
        // right physical model here: the BSSs sit on non-overlapping channels (2.4 vs
        // 5 GHz in rftuning, distinct 5 GHz channels for the two ax APs), so they do not
        // interfere. Co-channel interference between BSSs is therefore out of scope;
        // intra-BSS contention, which is what the thesis measures, is unaffected.
        BuildBss(bssList[b],
                 BuildSpectrumChannel(propagation, exponent, refLoss),
                 bssStandard[b],
                 rateManager,
                 bssOfdma[b],
                 txPowerDbm,
                 apNodes.Get(bssList[b].apNodeIndex),
                 members,
                 enablePcap,
                 outDir + "/" + tag + "-bss" + std::to_string(b));
    }

    int64_t stream = 200;
    for (const auto& bss : bssList)
    {
        stream += WifiHelper::AssignStreams(bss.apDevice, stream);
        stream += WifiHelper::AssignStreams(bss.staDevices, stream);
    }

    // ---------------------------------------------------------------------
    // Stage 5: wired backhaul, IP stack and addressing
    // ---------------------------------------------------------------------
    PointToPointHelper backhaul;
    backhaul.SetDeviceAttribute("DataRate", StringValue(backhaulRate));
    backhaul.SetChannelAttribute("Delay", StringValue(backhaulDelay));

    std::vector<NetDeviceContainer> apToCore;
    for (uint32_t a = 0; a < nAp; ++a)
    {
        apToCore.push_back(backhaul.Install(NodeContainer(apNodes.Get(a), coreNode.Get(0))));
    }
    NetDeviceContainer coreToServer =
        backhaul.Install(NodeContainer(coreNode.Get(0), serverNode.Get(0)));

    InternetStackHelper internet;
    internet.Install(apNodes);
    internet.Install(staNodes);
    internet.Install(coreNode);
    internet.Install(serverNode);

    Ipv4AddressHelper address;
    std::vector<Ipv4Address> staAddress(nSta);

    for (size_t b = 0; b < bssList.size(); ++b)
    {
        if (bssList[b].staDevices.GetN() == 0)
        {
            continue;
        }
        std::ostringstream base;
        base << "10.1." << (b + 1) << ".0";
        address.SetBase(base.str().c_str(), "255.255.255.0");
        address.Assign(bssList[b].apDevice);
        Ipv4InterfaceContainer staIfaces = address.Assign(bssList[b].staDevices);
        for (uint32_t k = 0; k < bssList[b].staIndices.size(); ++k)
        {
            staAddress[bssList[b].staIndices[k]] = staIfaces.GetAddress(k);
        }
    }

    for (uint32_t a = 0; a < nAp; ++a)
    {
        std::ostringstream base;
        base << "10.10." << (a + 1) << ".0";
        address.SetBase(base.str().c_str(), "255.255.255.0");
        address.Assign(apToCore[a]);
    }

    address.SetBase("10.20.1.0", "255.255.255.0");
    Ipv4InterfaceContainer serverIfaces = address.Assign(coreToServer);
    const Ipv4Address serverAddress = serverIfaces.GetAddress(1);

    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    // ---------------------------------------------------------------------
    // Stage 6: applications
    // ---------------------------------------------------------------------
    const bool wantDownlink = (direction == "downlink" || direction == "both");
    const bool wantUplink = (direction == "uplink" || direction == "both");
    NS_ABORT_MSG_IF(!wantDownlink && !wantUplink,
                    "Unknown direction '" << direction << "'. Use downlink, uplink or both.");

    ApplicationContainer sources;
    ApplicationContainer sinks;
    const Time appStart = Seconds(1.0);
    const Time appStop = appStart + simTime;

    for (uint32_t i = 0; i < nSta; ++i)
    {
        const TrafficClass& tc = ClassifyStation(i, nSta, browsingShare, videoShare);

        if (wantDownlink)
        {
            const uint16_t port = DOWNLINK_BASE_PORT + static_cast<uint16_t>(i);
            const InetSocketAddress dest(staAddress[i], port);

            // The Tos attribute is what maps the flow onto a Wi-Fi access category;
            // InetSocketAddress itself carries no TOS field in ns-3.
            OnOffHelper onOff("ns3::UdpSocketFactory", dest);
            onOff.SetAttribute("DataRate", DataRateValue(DataRate(tc.rateBps)));
            onOff.SetAttribute("PacketSize", UintegerValue(tc.packetBytes));
            onOff.SetAttribute("Tos", UintegerValue(tc.tos));
            // Web traffic is bursty; video and voice are effectively constant bit rate.
            if (tc.name == "browsing")
            {
                onOff.SetAttribute("OnTime",
                                   StringValue("ns3::ExponentialRandomVariable[Mean=1.0]"));
                onOff.SetAttribute("OffTime",
                                   StringValue("ns3::ExponentialRandomVariable[Mean=1.0]"));
            }
            else
            {
                onOff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
                onOff.SetAttribute("OffTime",
                                   StringValue("ns3::ConstantRandomVariable[Constant=0]"));
            }
            sources.Add(onOff.Install(serverNode.Get(0)));

            PacketSinkHelper sink("ns3::UdpSocketFactory",
                                  InetSocketAddress(Ipv4Address::GetAny(), port));
            sinks.Add(sink.Install(staNodes.Get(i)));
        }

        if (wantUplink)
        {
            const uint16_t port = UPLINK_BASE_PORT + static_cast<uint16_t>(i);
            const uint64_t upRate =
                std::max<uint64_t>(16000, static_cast<uint64_t>(tc.rateBps * uplinkRatio));
            const InetSocketAddress dest(serverAddress, port);

            OnOffHelper onOff("ns3::UdpSocketFactory", dest);
            onOff.SetAttribute("DataRate", DataRateValue(DataRate(upRate)));
            onOff.SetAttribute("PacketSize", UintegerValue(tc.packetBytes));
            onOff.SetAttribute("Tos", UintegerValue(tc.tos));
            onOff.SetAttribute("OnTime", StringValue("ns3::ConstantRandomVariable[Constant=1]"));
            onOff.SetAttribute("OffTime", StringValue("ns3::ConstantRandomVariable[Constant=0]"));
            sources.Add(onOff.Install(staNodes.Get(i)));

            PacketSinkHelper sink("ns3::UdpSocketFactory",
                                  InetSocketAddress(Ipv4Address::GetAny(), port));
            sinks.Add(sink.Install(serverNode.Get(0)));
        }
    }

    sinks.Start(Seconds(0.0));
    sinks.Stop(appStop + Seconds(1.0));
    // Staggering the start avoids an artificial synchronised burst at t = 1 s.
    sources.StartWithJitter(appStart, CreateObject<UniformRandomVariable>());
    sources.Stop(appStop);

    // ---------------------------------------------------------------------
    // Stage 7: measurement
    // ---------------------------------------------------------------------
    FlowMonitorHelper flowmonHelper;
    Ptr<FlowMonitor> monitor = flowmonHelper.InstallAll();

    Simulator::Stop(appStop + Seconds(2.0));
    Simulator::Run();

    monitor->CheckForLostPackets();
    auto classifier = DynamicCast<Ipv4FlowClassifier>(flowmonHelper.GetClassifier());
    const auto stats = monitor->GetFlowStats();

    monitor->SerializeToXmlFile(outDir + "/" + tag + "-flowmon.xml", true, true);

    std::ofstream flowCsv(outDir + "/" + tag + "-flows.csv");
    flowCsv << "flow_id,direction,traffic_class,src_addr,dst_addr,src_port,dst_port,tx_packets,"
               "rx_packets,lost_packets,tx_bytes,rx_bytes,throughput_mbps,mean_delay_ms,"
               "mean_jitter_ms,loss_pct\n";
    flowCsv << std::fixed << std::setprecision(6);

    // Per-class accumulators plus one for the whole run, so the dashboard can compare the QoS
    // that each application class actually received.
    std::map<std::string, QosAccumulator> perClass;
    QosAccumulator overall;
    overall.stations = nSta;
    for (uint32_t i = 0; i < nSta; ++i)
    {
        perClass[ClassifyStation(i, nSta, browsingShare, videoShare).name].stations++;
    }

    for (const auto& [flowId, s] : stats)
    {
        const auto t = classifier->FindFlow(flowId);
        const bool isUplink = (t.destinationAddress == serverAddress);

        // Recover which station, and therefore which traffic class, this flow belongs to.
        const uint32_t base = isUplink ? UPLINK_BASE_PORT : DOWNLINK_BASE_PORT;
        const auto port = static_cast<uint32_t>(t.destinationPort);
        std::string className = "other";
        if (port >= base && port < base + nSta)
        {
            const uint32_t staIndex = port - base;
            className = ClassifyStation(staIndex, nSta, browsingShare, videoShare).name;
        }

        const double activeSeconds =
            (s.timeLastRxPacket - s.timeFirstTxPacket).GetSeconds() > 0.0
                ? (s.timeLastRxPacket - s.timeFirstTxPacket).GetSeconds()
                : simTime.GetSeconds();
        const double throughputMbps = (s.rxBytes * 8.0) / activeSeconds / 1e6;
        const double meanDelayMs =
            (s.rxPackets > 0) ? s.delaySum.GetSeconds() * 1000.0 / s.rxPackets : 0.0;
        const double meanJitterMs =
            (s.rxPackets > 1) ? s.jitterSum.GetSeconds() * 1000.0 / (s.rxPackets - 1) : 0.0;
        const double lossPct =
            (s.txPackets > 0)
                ? 100.0 * static_cast<double>(s.txPackets - s.rxPackets) / s.txPackets
                : 0.0;

        flowCsv << flowId << "," << (isUplink ? "uplink" : "downlink") << "," << className << ","
                << t.sourceAddress << "," << t.destinationAddress << "," << t.sourcePort << ","
                << t.destinationPort << "," << s.txPackets << "," << s.rxPackets << ","
                << s.lostPackets << "," << s.txBytes << "," << s.rxBytes << "," << throughputMbps
                << "," << meanDelayMs << "," << meanJitterMs << "," << lossPct << "\n";

        for (QosAccumulator* acc : {&perClass[className], &overall})
        {
            acc->txPackets += s.txPackets;
            acc->rxPackets += s.rxPackets;
            acc->lostPackets += s.lostPackets;
            acc->rxBytes += s.rxBytes;
            acc->delaySumMs += s.delaySum.GetSeconds() * 1000.0;
            if (s.rxPackets > 1)
            {
                acc->jitterSumMs += s.jitterSum.GetSeconds() * 1000.0;
                acc->jitterSamples += s.rxPackets - 1;
            }
            acc->flows++;
        }
    }
    flowCsv.close();

    const std::string summaryPath = outDir + "/summary.csv";
    const bool needHeader = !std::filesystem::exists(summaryPath);
    std::ofstream summary(summaryPath, std::ios::app);
    if (needHeader)
    {
        summary << "scenario,zone,zone_label,n_sta,n_ap,n_bss,direction,rate_manager,propagation,"
                   "path_loss_exponent,reference_loss_db,radius_m,tx_power_dbm,use_rts,sim_time_s,"
                   "seed,run,traffic_class,flows,agg_throughput_mbps,per_sta_throughput_mbps,"
                   "mean_delay_ms,mean_jitter_ms,loss_pct,tx_packets,rx_packets,lost_packets\n";
    }
    summary << std::fixed << std::setprecision(6);

    // Emitted once per traffic class and once for the run as a whole (traffic_class = all).
    auto writeSummaryRow = [&](const std::string& className, const QosAccumulator& acc) {
        const double aggThroughputMbps = (acc.rxBytes * 8.0) / simTime.GetSeconds() / 1e6;
        const double perStaThroughputMbps =
            (acc.stations > 0) ? aggThroughputMbps / acc.stations : 0.0;
        const double meanDelayMs =
            (acc.rxPackets > 0) ? acc.delaySumMs / acc.rxPackets : 0.0;
        const double meanJitterMs =
            (acc.jitterSamples > 0) ? acc.jitterSumMs / acc.jitterSamples : 0.0;
        const double lossPct =
            (acc.txPackets > 0)
                ? 100.0 * static_cast<double>(acc.txPackets - acc.rxPackets) / acc.txPackets
                : 0.0;

        summary << scenario << "," << zone << ",\"" << preset.label << "\"," << nSta << "," << nAp
                << "," << bssList.size() << "," << direction << "," << rateManager << ","
                << propagation << "," << exponent << "," << refLoss << "," << radiusM << ","
                << txPowerDbm << "," << (useRts ? 1 : 0) << "," << simTime.GetSeconds() << ","
                << seed << "," << run << "," << className << "," << acc.flows << ","
                << aggThroughputMbps << "," << perStaThroughputMbps << "," << meanDelayMs << ","
                << meanJitterMs << "," << lossPct << "," << acc.txPackets << "," << acc.rxPackets
                << "," << acc.lostPackets << "\n";

        if (className != "all")
        {
            std::cout << "  " << std::left << std::setw(10) << className << std::right
                      << std::fixed << std::setprecision(2) << "  throughput " << std::setw(9)
                      << aggThroughputMbps << " Mbps   latency " << std::setw(8) << meanDelayMs
                      << " ms   jitter " << std::setw(7) << meanJitterMs << " ms   loss "
                      << std::setw(6) << lossPct << " %\n";
        }
    };

    std::cout << "\n=== MSU-IIT campus Wi-Fi simulation ===\n"
              << "  scenario           : " << scenario << "\n"
              << "  zone               : " << preset.label << " (" << zone << ")\n"
              << "  stations / APs     : " << nSta << " / " << nAp << "\n"
              << "  BSSs               : " << bssList.size() << "\n"
              << "  direction          : " << direction << "\n"
              << "  rate manager       : " << rateManager << "\n"
              << "  propagation        : " << propagation << " (exponent " << exponent << ")\n"
              << "--- per traffic class -----------------\n";

    for (const char* const className : {"browsing", "video", "voip"})
    {
        if (perClass.count(className) > 0)
        {
            writeSummaryRow(className, perClass[className]);
        }
    }
    if (perClass.count("other") > 0)
    {
        writeSummaryRow("other", perClass["other"]);
    }
    writeSummaryRow("all", overall);
    summary.close();

    const double aggThroughputMbps = (overall.rxBytes * 8.0) / simTime.GetSeconds() / 1e6;
    const double meanDelayMs =
        (overall.rxPackets > 0) ? overall.delaySumMs / overall.rxPackets : 0.0;
    const double meanJitterMs =
        (overall.jitterSamples > 0) ? overall.jitterSumMs / overall.jitterSamples : 0.0;
    const double lossPct =
        (overall.txPackets > 0)
            ? 100.0 * static_cast<double>(overall.txPackets - overall.rxPackets) / overall.txPackets
            : 0.0;
    const double perStaThroughputMbps = aggThroughputMbps / nSta;
    const uint32_t flowCount = overall.flows;

    std::cout << "--- whole run -------------------------\n"
              << std::fixed << std::setprecision(3)
              << "  aggregate throughput : " << aggThroughputMbps << " Mbps\n"
              << "  per-station          : " << perStaThroughputMbps << " Mbps\n"
              << "  mean latency         : " << meanDelayMs << " ms\n"
              << "  mean jitter          : " << meanJitterMs << " ms\n"
              << "  packet loss          : " << lossPct << " %\n"
              << "  flows                : " << flowCount << "\n"
              << "---------------------------------------\n"
              << "  summary appended to  : " << summaryPath << "\n"
              << "  per-flow CSV         : " << outDir << "/" << tag << "-flows.csv\n"
              << "  FlowMonitor XML      : " << outDir << "/" << tag << "-flowmon.xml\n\n";

    Simulator::Destroy();
    return 0;
}
