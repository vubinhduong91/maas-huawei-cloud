-- Hub-fix P5: a stable-order JSON encoder for a table decoded by APISIX's core.json.decode
-- (lua-cjson with decode_array_with_array_mt). APISIX's core.json.stably_encode is dkjson's
-- encode, which sorts keys but, used as it is on such a table:
--   * aborts on JSON null (cjson.null is a userdata dkjson does not know) -> HTTP 500;
--   * writes every EMPTY table as [] (it ignores cjson's array_mt), so an empty object such as
--     a tool's "properties": {} or "additionalProperties": {} becomes [].
-- encode(t) marks the empty objects for dkjson, writes cjson.null as null and sorts the keys
-- (dkjson sorts only by default, i.e. when no state is given, so sortkey is set here).
-- Numbers are still printed with 14 significant digits (9007199254740991 -> 9.007199254741e+15).
local cjson = require("cjson.safe")
local dkjson = require("dkjson")

local _M = {}

local OBJECT = { __jsontype = "object" }
local ARRAY_MT = cjson.array_mt
local NULL = cjson.null

-- dkjson writes a table as an array when its keys are 1..n, so only EMPTY objects need its
-- __jsontype = "object" mark; empty arrays keep cjson's array_mt and are written as [].
local function mark(t)
    if next(t) == nil then
        if getmetatable(t) ~= ARRAY_MT then setmetatable(t, OBJECT) end
        return
    end
    for _, v in pairs(t) do
        if type(v) == "table" then mark(v) end
    end
end

local function exception(reason, value, state, default_message)
    if value == NULL then return "null" end
    return nil, default_message or reason
end

-- Note: marks the empty objects of t in place.
function _M.encode(t)
    mark(t)
    return dkjson.encode(t, { sortkey = true, exception = exception })
end

return _M
