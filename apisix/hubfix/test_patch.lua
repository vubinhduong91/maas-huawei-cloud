-- Unit tests for apisix/hubfix/patch.lua (P4), on the fixtures of scenarios/hubfix_fixtures.py.
-- Runs with the LuaJIT and lua-cjson of the APISIX image (make test-hubfix):
--   luajit test_patch.lua <fixtures dir>
-- For every case: status as expected; patched -> output bytes equal <name>.out; fallback ->
-- reason contains the expected text and no body is returned. Also prints the time per call.
package.path = arg[0]:gsub("[^/]*$", "") .. "?.lua;" .. package.path
package.cpath = "/usr/local/openresty/lualib/?.so;" .. package.cpath
local cjson = require("cjson.safe")
cjson.decode_array_with_array_mt(true)   -- as apisix/core/json.lua
local hubfix = require("patch")

local dir = arg[1] or "/fixtures"
local function read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return s
end

local index = cjson.decode(read(dir .. "/index.json"))
local opts = { decode = cjson.decode, glm_model = "glm-5.3" }
local passed, failed = 0, 0
for _, c in ipairs(index) do
    local raw = read(dir .. "/" .. c.name .. ".in")
    local reps = #raw > 100000 and 5 or 20
    local t0 = os.clock()
    local status, out, reason
    for _ = 1, reps do status, out, reason = hubfix.patch(raw, opts) end
    local ms = (os.clock() - t0) * 1000 / reps
    local ok, why = status == c.expect, "status " .. tostring(status) .. ", expected " .. c.expect
    if ok and status == "patched" then
        local want = read(dir .. "/" .. c.name .. ".out")
        ok = out == want
        why = "patched bytes differ from " .. c.name .. ".out"
    elseif ok and status == "fallback" then
        ok = out == nil and type(reason) == "string" and reason:find(c.reason, 1, true) ~= nil
        why = "fallback reason " .. tostring(reason) .. ", expected to contain " .. c.reason
    elseif ok then
        ok = out == nil
        why = "unchanged must not return a body"
    end
    if ok then passed = passed + 1 else failed = failed + 1 end
    print(string.format("%-4s %-40s %9d B  %-9s %7.2f ms  %s", ok and "ok" or "FAIL", c.name, #raw,
        tostring(status), ms, ok and (reason or "") or why))
end
print(string.format("%d/%d passed", passed, passed + failed))
os.exit(failed == 0 and 0 or 1)
