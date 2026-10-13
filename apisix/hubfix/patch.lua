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
-- Steps: locate the two values at depth 1 and read them; splice their bytes; decode the result
-- once and check the mapped values (v2: the v1 full decode of the input, ~40% of the time on
-- a 160K-token body, is gone; the scan reads the values itself).
local byte, find, sub = string.byte, string.find, string.sub

local _M = { version = "2" }

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
-- top-level "output_config" object (nil when absent). Errors (-> fallback): a key that appears
-- twice, and one of these keys written with escapes (e.g. "model": decoders see "model",
-- but its bytes cannot be matched safely).
local NAMES = { model = true, output_config = true, effort = true }

local function key_name(key, decode)
    if not find(key, "\\", 1, true) then return key end
    local ok, k = pcall(decode, '"' .. key .. '"')
    if ok and NAMES[k] then error("escaped key " .. k, 0) end
    return key
end

local function locate(s, decode)
    local start = skip_ws(s, 1)
    if not start or byte(s, start) ~= LBRACE then error("body is not a JSON object", 0) end
    local spans, seen = {}, {}
    local close = each_member(s, start, function(raw_key, v1, v2)
        local key = key_name(raw_key, decode)
        if key == "model" or key == "output_config" then
            if seen[key] then error("duplicate top-level key " .. key, 0) end
            seen[key] = true
        end
        if key == "model" then
            spans.model = { v1, v2 }
        elseif key == "output_config" and byte(s, v1) == LBRACE then
            local seen_effort = false
            each_member(s, v1, function(raw_k, w1, w2)
                if key_name(raw_k, decode) == "effort" then
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
    local first = skip_ws(raw, 1)
    if not first or byte(raw, first) ~= LBRACE then return "unchanged", nil, nil end   -- nothing to map

    -- 1. Locate the two values at depth 1 and read them (each is a short JSON scalar).
    local ok, spans = pcall(locate, raw, decode)
    if not ok then return "fallback", nil, "scan: " .. tostring(spans) end
    local function value(name)
        local span = spans[name]
        if not span then return nil end
        local okv, v = pcall(decode, sub(raw, span[1], span[2]))
        return okv and v or nil
    end
    local old_model, old_effort = value("model"), value("effort")

    -- 2. What the mapping asks for.
    local new_model, new_effort
    if type(old_model) == "string" and sub(old_model, 1, #prefix) == prefix then
        new_model = opts.glm_model
    end
    if type(old_effort) == "string" then
        new_effort = effort_map[old_effort]
    end
    local final_model = new_model or old_model
    if type(final_model) == "string" and sub(final_model, 1, 7) == "claude-" then
        return "unchanged", nil, nil   -- other Claude models (haiku, opus) are not sent to MaaS
    end
    if not new_model and not new_effort then return "unchanged", nil, nil end

    -- 3. Splice: only the bytes of those values change, from the end of the body backwards.
    local edits = {}
    for _, e in ipairs({ { "model", old_model, new_model }, { "effort", old_effort, new_effort } }) do
        local name, old, new = e[1], e[2], e[3]
        if new then
            local span = spans[name]
            -- The old value only has to be found byte for byte: a name like "claude-sonnet-5-5[1m]"
            -- is fine, and one written with escapes never matches '"' .. old .. '"' -> fallback.
            local old_json, new_json = type(old) == "string" and '"' .. old .. '"', json_string(new)
            if not old_json or not new_json then return "fallback", nil, name .. " value is not a plain string" end
            if sub(raw, span[1], span[2]) ~= old_json then
                return "fallback", nil, name .. " bytes do not match the decoded value"
            end
            edits[#edits + 1] = { span[1], span[2], new_json }
        end
    end
    table.sort(edits, function(x, y) return x[1] > y[1] end)
    local out = raw
    for _, e in ipairs(edits) do
        out = sub(out, 1, e[1] - 1) .. e[3] .. sub(out, e[2] + 1)
    end

    -- 4. Confirm with one decode of the result: valid JSON whose top-level model and
    -- output_config.effort hold the mapped values. Nothing else can differ: only the two
    -- spans above were replaced, and their old bytes were checked against the old values.
    local ok3, check = pcall(decode, out)
    if not ok3 or type(check) ~= "table" then return "fallback", nil, "patched body does not decode" end
    if check.model ~= final_model then return "fallback", nil, "check: model" end
    if new_effort then
        local oc = check.output_config
        if type(oc) ~= "table" or oc.effort ~= new_effort then return "fallback", nil, "check: effort" end
    end
    return "patched", out, nil
end

-- exposed for the unit tests
_M._locate = locate

return _M
