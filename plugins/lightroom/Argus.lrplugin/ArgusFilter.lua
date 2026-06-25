--[[ Export filter: analyze the export directory via Argus HTTP API.
     Settings live in plug-in preferences (Plug-in Manager).
--]]

local LrPathUtils = import "LrPathUtils"
local LrFileUtils = import "LrFileUtils"
local LrTasks = import "LrTasks"
local LrDialogs = import "LrDialogs"
local LrApplication = import "LrApplication"
local LrBinding = import "LrBinding"
local LrView = import "LrView"
local LrPrefs = import "LrPrefs"

local prefs = LrPrefs.prefsForPlugin()

local function pref(key, default)
    local v = prefs[key]
    if v == nil or v == "" then
        return default
    end
    return v
end

local function quote(arg)
    if arg == nil then
        return '""'
    end
    return '"' .. tostring(arg):gsub('"', '\\"') .. '"'
end

local exportFilterProvider = {}

function exportFilterProvider.sectionForFilterInDialog(f, propertyTable)
    return {
        title = "Argus vision analyze",
        f:row {
            f:static_text {
                title = "Analyze exported files with Argus (tailnet). Configure Python + script in Plug-in Manager.",
                fill_horizontal = 1,
            },
        },
        f:row {
            f:static_text { title = "Limit:", width = LrView.share "label_width" },
            f:edit_field {
                value = LrView.bind { key = "argusLimit", object = propertyTable },
                width_in_chars = 6,
                validate = function(v)
                    local n = tonumber(v)
                    if n == nil or n < 0 then
                        return false, "Enter 0 for all photos or a positive limit"
                    end
                    return true
                end,
            },
        },
        f:row {
            f:static_text { title = "Recursive:", width = LrView.share "label_width" },
            f:checkbox {
                value = LrView.bind { key = "argusRecursive", object = propertyTable },
                title = "Scan subfolders",
            },
        },
    }
end

function exportFilterProvider.startDialog(propertyTable)
    propertyTable.argusLimit = propertyTable.argusLimit or tonumber(pref("limit", 0))
    if propertyTable.argusRecursive == nil then
        propertyTable.argusRecursive = pref("recursive", false)
    end
end

function exportFilterProvider.updateFilterPreset(propertyTable)
    prefs.limit = propertyTable.argusLimit
    prefs.recursive = propertyTable.argusRecursive
end

function exportFilterProvider.processRenderedPhotos(functionContext, exportContext)
    local exportSettings = exportContext.exportSessionSettings
    local exportPath = exportSettings.LR_export_destinationPath
    if not exportPath or exportPath == "" then
        LrDialogs.message("Argus", "Export path missing — cannot analyze.")
        return
    end

    local pythonBin = pref("python", "python3")
    local scriptPath = pref("script", "")
    local baseUrl = pref("base_url", "http://127.0.0.1:8010")
    local token = pref("api_token", "")
    local clientId = pref("client_id", "")
    local limit = tonumber(exportContext.propertyTable.argusLimit) or tonumber(pref("limit", 0))
    local recursive = exportContext.propertyTable.argusRecursive
    if recursive == nil then
        recursive = pref("recursive", false)
    end

    if scriptPath == "" or not LrFileUtils.exists(scriptPath) then
        LrDialogs.message(
            "Argus",
            "Set a valid Argus script path in File → Plug-in Manager → Argus Vision → Plug-in Preferences."
        )
        return
    end

    local cmdParts = {
        quote(pythonBin),
        quote(scriptPath),
        quote(exportPath),
        "--base-url", quote(baseUrl),
        "--limit", tostring(limit),
        "--target-dir", quote(exportPath),
    }
    if token ~= "" then
        table.insert(cmdParts, "--token")
        table.insert(cmdParts, quote(token))
    end
    if clientId ~= "" then
        table.insert(cmdParts, "--client-id")
        table.insert(cmdParts, quote(clientId))
    end
    if recursive then
        table.insert(cmdParts, "--recursive")
    end
    table.insert(cmdParts, "--queue")
    table.insert(cmdParts, "--max-wait")
    table.insert(cmdParts, "7200")
    local manifestOut = LrPathUtils.child(exportPath, "argus-manifest.json")
    table.insert(cmdParts, "--manifest-out")
    table.insert(cmdParts, quote(manifestOut))

    local command = table.concat(cmdParts, " ")

    LrTasks.startAsyncTask(function()
        LrTasks.sleep(1)
        local ok, exitCode = LrTasks.execute(command)
        if not ok or (exitCode and exitCode ~= 0) then
            LrDialogs.message(
                "Argus",
                "Export analyze failed (exit " .. tostring(exitCode) .. "). Check Plug-in Preferences and tailnet URL."
            )
        else
            LrDialogs.message("Argus", "Analysis complete. Sidecars written beside export files.")
        end
    end)
end

return exportFilterProvider