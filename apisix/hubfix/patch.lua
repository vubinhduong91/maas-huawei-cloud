-- Hub-fix P4: map the Claude Code request for a GLM model on Huawei MaaS by replacing only the
-- bytes of the values that must change, never re-encoding the body (re-encoding with lua-cjson
-- changes the key order and the numbers, which breaks MaaS prompt caching).
--
--   model                  "claude-sonnet-*"   -> opts.glm_model (e.g. "glm-5.3")
--   output_config.effort   "medium" -> "high", "xhigh" -> "max"   (glm-5.3 accepts low/high/max)
-- Requests that keep a claude-* model (haiku, opus: not sent to MaaS) are left untouched.
--
-- Both are TOP-LEVEL fields. The same key names also appear deeper in a Claude Code body
-- (tool schemas, messages[1].output_config, tool_use.input), so the body is scanned as JSON at
-- depth 1 only: strings (with escapes) and nested objects/arrays are skipped whole.
--
-- patch(raw, opts) returns status, body, reason:
--   "unchanged", nil, nil      nothing to map: forward raw as it is
--   "patched",   out, nil      forward out (raw with only those value bytes replaced)
--   "fallback",  nil, reason   could not locate or confirm the bytes: forward raw as it is and
--                              log reason. The whole body is NEVER re-encoded as a fallback.
-- opts: decode (a JSON decoder, e.g. core.json.decode or cjson.decode), glm_model,
--       sonnet_prefix (default "claude-sonnet-"), effort_map (default medium->high, xhigh->max).
-- Steps: decode only to read the values; locate their bytes in raw; splice; decode the result
-- and check that it equals the original document with exactly those values changed.
local byte, find, sub = string.byte, string.find, string.sub

local _M = { version = "1" }

local DEFAULT_EFFORT_MAP = { medium = "high", xhigh = "max" }
local QUOTE, BACKSLASH, LBRACE, RBRACE, LBRACKET, RBRACKET, COLON, COMMA =
    34, 92, 123, 125, 91, 93, 58, 44

local function skip_ws(s, i)
    return find(s, "[^ \t\r\n]", i)
end

-- i at the opening quote; returns the index of the closing quote. Jumps from quote to quote
-- (message text is full of \n and \" escapes): a quote preceded by an odd number of
-- backslashes is escaped, an even number (0, or \\ pairs) ends the string.
local function skip_string(s, i)
    local j = i + 1
    while true do
        local k = find(s, '"', j, true)
        if not k then error("unterminated string", 0) end
        local n = 0
        while byte(s, k - 1 - n) == BACKSLASH do n = n + 1 end
        if n % 2 == 0 then return k end
        j = k + 1
    end
end

-- i at the first byte of a value; returns the index of its last byte
local function skip_value(s, i)
    local c = byte(s, i)
    if c == QUOTE then
        return skip_string(s, i)
    elseif c == LBRACE or c == LBRACKET then
        local depth, j = 0, i
        while true do
            local k = find(s, '["{}%[%]]', j)
            if not k then error("unterminated container", 0) end
            local d = byte(s, k)
            if d == QUOTE then
                k = skip_string(s, k)
            elseif d == LBRACE or d == LBRACKET then
                depth = depth + 1
            else
                depth = depth - 1
                if depth == 0 then return k end
            end
            j = k + 1
        end
    else
        local k = find(s, "[,}%] \t\r\n]", i)   -- number, true, false, null
        if not k then error("unterminated literal", 0) end
        return k - 1
    end
end

-- Members of the object whose "{" is at i: on_member(raw_key, value_first, value_last).
-- Returns the index of the closing "}".
local function each_member(s, i, on_member)
    local j = skip_ws(s, i + 1)
    if not j then error("unterminated object", 0) end
    if byte(s, j) == RBRACE then return j end
    while true do
        if byte(s, j) ~= QUOTE then error("expected a key", 0) end
        local key_end = skip_string(s, j)
        local key = sub(s, j + 1, key_end - 1)          -- raw key bytes, escapes not decoded
        local colon = skip_ws(s, key_end + 1)
        if not colon or byte(s, colon) ~= COLON then error("expected ':'", 0) end
        local v1 = skip_ws(s, colon + 1)
        if not v1 then error("expected a value", 0) end
        local v2 = skip_value(s, v1)
        on_member(key, v1, v2)
        j = skip_ws(s, v2 + 1)
        if not j then error("unterminated object", 0) end
        local c = byte(s, j)
        if c == RBRACE then return j end
        if c ~= COMMA then error("expected ',' or '}'", 0) end
        j = skip_ws(s, j + 1)
        if not j then error("unterminated object", 0) end
    end
end

-- Byte spans {first, last} of the top-level "model" value and of "effort" inside the
-- top-level "output_config" object (nil when absent). A key that appears twice is an error.
local function locate(s)
    local start = skip_ws(s, 1)
    if not start or byte(s, start) ~= LBRACE then error("body is not a JSON object", 0) end
    local spans, seen = {}, {}
    local close = each_member(s, start, function(key, v1, v2)
        if key == "model" or key == "output_config" then
            if seen[key] then error("duplicate top-level key " .. key, 0) end
            seen[key] = true
        end
        if key == "model" then
            spans.model = { v1, v2 }
        elseif key == "output_config" and byte(s, v1) == LBRACE then
            local seen_effort = false
            each_member(s, v1, function(k, w1, w2)
                if k == "effort" then
                    if seen_effort then error("duplicate key output_config.effort", 0) end
                    seen_effort = true
                    spans.effort = { w1, w2 }
                end
            end)
        end
    end)
    if skip_ws(s, close + 1) then error("trailing bytes after the JSON object", 0) end
    return spans
end

local function deep_equal(a, b)
    if a == b then return true end
    if type(a) ~= "table" or type(b) ~= "table" then return false end
    for k, v in pairs(a) do
        if not deep_equal(v, b[k]) then return false end
    end
    for k in pairs(b) do
        if a[k] == nil then return false end
    end
    return getmetatable(a) == getmetatable(b)   -- cjson marks empty arrays with a metatable
end

local function json_string(v)
    -- Only plain ASCII names and effort levels are ever written: no escaping needed.
    if type(v) ~= "string" or not find(v, "^[%w%._%-]+$") then return nil end
    return '"' .. v .. '"'
end

function _M.patch(raw, opts)
    local decode = opts.decode
    local prefix = opts.sonnet_prefix or "claude-sonnet-"
    local effort_map = opts.effort_map or DEFAULT_EFFORT_MAP
    if type(raw) ~= "string" or raw == "" then return "fallback", nil, "empty body" end

    local ok, req = pcall(decode, raw)
    if not ok or type(req) ~= "table" then return "fallback", nil, "body is not valid JSON" end

    local new_model, new_effort
    if type(req.model) == "string" and sub(req.model, 1, #prefix) == prefix then
        new_model = opts.glm_model
    end
    local oc = req.output_config
    if type(oc) == "table" and type(oc.effort) == "string" then
        new_effort = effort_map[oc.effort]
    end
    local final_model = new_model or req.model
    if type(final_model) == "string" and sub(final_model, 1, 7) == "claude-" then
        return "unchanged", nil, nil   -- other Claude models (haiku, opus) are not sent to MaaS
    end
    if not new_model and not new_effort then return "unchanged", nil, nil end

    local ok2, spans = pcall(locate, raw)
    if not ok2 then return "fallback", nil, "scan: " .. tostring(spans) end

    local edits = {}
    for _, e in ipairs({ { "model", req.model, new_model }, { "effort", oc and oc.effort, new_effort } }) do
        local name, old, new = e[1], e[2], e[3]
        if new then
            local span = spans[name]
            if not span then return "fallback", nil, name .. " not found at the expected depth" end
            local old_json, new_json = json_string(old), json_string(new)
            if not old_json or not new_json then return "fallback", nil, name .. " value is not a plain string" end
            if sub(raw, span[1], span[2]) ~= old_json then
                return "fallback", nil, name .. " bytes do not match the decoded value"
            end
            edits[#edits + 1] = { span[1], span[2], new_json }
        end
    end
    table.sort(edits, function(x, y) return x[1] > y[1] end)   -- splice from the end
    local out = raw
    for _, e in ipairs(edits) do
        out = sub(out, 1, e[1] - 1) .. e[3] .. sub(out, e[2] + 1)
    end

    -- Confirm: the result is the original document with exactly those values changed.
    local ok3, check = pcall(decode, out)
    if not ok3 or type(check) ~= "table" then return "fallback", nil, "patched body does not decode" end
    if new_model then req.model = new_model end
    if new_effort then oc.effort = new_effort end
    if not deep_equal(req, check) then return "fallback", nil, "patched body differs beyond the mapped fields" end
    return "patched", out, nil
end

-- exposed for the unit tests
_M._locate = locate
_M._deep_equal = deep_equal

return _M
