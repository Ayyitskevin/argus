--[[ Argus Vision — Lightroom Classic export filter (Phase 8).
     Post-export: shell to docs/lightroom_export_stub.py on the tailnet host.
--]]

return {
    LrSdkVersion = 6.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = "com.kleephotography.argus",
    LrPluginName = "Argus Vision",
    LrPluginInfoUrl = "https://github.com/Ayyitskevin/argus",
    LrExportFilterProvider = {
        title = "Argus vision analyze",
        file = "ArgusFilter.lua",
    },
    LrPluginInfoProvider = {
        file = "PluginInit.lua",
    },
    VERSION = { major = 1, minor = 0, revision = 0, build = 0 },
}