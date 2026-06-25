--[[ Plug-in Manager preferences for Argus Vision. --]]

local LrView = import "LrView"
local LrBinding = import "LrBinding"
local LrPrefs = import "LrPrefs"

local prefs = LrPrefs.prefsForPlugin()

return {
    sectionsForTopOfDialog = function(viewFactory, propertyTable)
        propertyTable.python = prefs.python or "python3"
        propertyTable.script = prefs.script or ""
        propertyTable.base_url = prefs.base_url or "http://127.0.0.1:8010"
        propertyTable.api_token = prefs.api_token or ""
        propertyTable.client_id = prefs.client_id or ""
        propertyTable.limit = prefs.limit or 0
        propertyTable.recursive = prefs.recursive or false

        return {
            title = "Argus connection",
            viewFactory:row {
                viewFactory:static_text {
                    title = "Python, script path, tailnet URL, and bearer token for post-export analyze.",
                    fill_horizontal = 1,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Python:", width = 120 },
                viewFactory:edit_field {
                    value = LrView.bind { key = "python", object = propertyTable },
                    width_in_chars = 40,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Argus script:", width = 120 },
                viewFactory:edit_field {
                    value = LrView.bind { key = "script", object = propertyTable },
                    width_in_chars = 50,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Base URL:", width = 120 },
                viewFactory:edit_field {
                    value = LrView.bind { key = "base_url", object = propertyTable },
                    width_in_chars = 40,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "API token:", width = 120 },
                viewFactory:password_field {
                    value = LrView.bind { key = "api_token", object = propertyTable },
                    width_in_chars = 40,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Client ID:", width = 120 },
                viewFactory:edit_field {
                    value = LrView.bind { key = "client_id", object = propertyTable },
                    width_in_chars = 30,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Default limit:", width = 120 },
                viewFactory:edit_field {
                    value = LrView.bind { key = "limit", object = propertyTable },
                    width_in_chars = 8,
                },
            },
            viewFactory:row {
                viewFactory:static_text { title = "Recursive:", width = 120 },
                viewFactory:checkbox {
                    value = LrView.bind { key = "recursive", object = propertyTable },
                    title = "Scan subfolders by default",
                },
            },
        }
    end,

    sectionsForBottomOfDialog = function()
        return {}
    end,

    updateFromDialog = function(propertyTable)
        prefs.python = propertyTable.python
        prefs.script = propertyTable.script
        prefs.base_url = propertyTable.base_url
        prefs.api_token = propertyTable.api_token
        prefs.client_id = propertyTable.client_id
        prefs.limit = tonumber(propertyTable.limit) or 0
        prefs.recursive = propertyTable.recursive
    end,
}