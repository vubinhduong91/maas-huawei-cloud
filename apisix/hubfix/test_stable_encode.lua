-- Unit tests for apisix/hubfix/stable_encode.lua (P5) on the P4 fixtures (every *.in that is
-- valid JSON): the output decodes to the same document (same values, objects stay objects and
-- arrays stay arrays, numbers compared at 14 significant digits), the same input always gives
-- the same bytes, and a body with its keys in another order gives the same bytes too.
-- Also shows why APISIX's plain core.json.stably_encode is not enough. make test-hubfix.
package.path = arg[0]:gsub("[^/]*$", "") .. "?.lua;/usr/local/apisix/deps/share/lua/5.1/?.lua;" .. package.path
package.cpath = "/usr/local/openresty/lualib/?.so;" .. package.cpath
local cjson = require("cjson.safe")
cjson.decode_array_with_array_mt(true)   -- as apisix/core/json.lua
local stable = require("stable_encode")
local dkjson = require("dkjson")

local dir = arg[1] or "/fixtures"
local function read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return s
end

local function same(a, b)
    if type(a) == "number" and type(b) == "number" then
        return string.format("%.14g", a) == string.format("%.14g", b)
    end
    if type(a) ~= "table" or type(b) ~= "table" then return a == b end
    if (getmetatable(a) == cjson.array_mt) ~= (getmetatable(b) == cjson.array_mt) then return false end
    for k, v in pairs(a) do
        if not same(v, b[k]) then return false end
    end
    for k in pairs(b) do
        if a[k] == nil then return false end
    end
    return true
end

local index = cjson.decode(read(dir .. "/index.json"))
local passed, failed, plain_null, plain_empty = 0, 0, 0, 0
for _, c in ipairs(index) do
    local raw = read(dir .. "/" .. c.name .. ".in")
    local doc = cjson.decode(raw)
    if type(doc) == "table" then
        local t0 = os.clock()
        local out, err = stable.encode(cjson.decode(raw))
        local ms = (os.clock() - t0) * 1000
        local ok, why = out ~= nil, tostring(err)
        if ok then
            local back = cjson.decode(out)
            ok, why = same(doc, back), "decoded output differs from the input document"
        end
        if ok then
            ok, why = stable.encode(cjson.decode(raw)) == out, "not deterministic"
        end
        if ok and c.name:find("reversed%-key%-order") then
            local fwd = read(dir .. "/" .. c.name:gsub("reversed%-key%-order", "medium") .. ".in")
            ok, why = stable.encode(cjson.decode(fwd)) == out, "key order of the input changes the output"
        end
        if ok then passed = passed + 1 else failed = failed + 1 end
        -- plain core.json.stably_encode (dkjson.encode without state) on the same table
        local pok, pout = pcall(dkjson.encode, cjson.decode(raw))
        local note
        if not pok or not pout then
            plain_null = plain_null + 1
            note = "plain stably_encode fails: " .. tostring(pout)
        elseif not same(doc, cjson.decode(pout)) then
            plain_empty = plain_empty + 1
            note = "plain stably_encode changes the document ({} -> [])"
        end
        print(string.format("%-4s %-40s %9d B  %7.2f ms  %s", ok and "ok" or "FAIL", c.name, #raw, ms,
            ok and (note or "") or why))
    end
end
print(string.format("%d/%d passed; plain core.json.stably_encode: %d failed, %d changed the document",
    passed, passed + failed, plain_null, plain_empty))
os.exit(failed == 0 and 0 or 1)
