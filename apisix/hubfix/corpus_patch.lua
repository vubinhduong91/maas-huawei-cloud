-- Corpus run of apisix/hubfix/patch.lua (P4) for scenarios/hubfix_corpus.py.
--   luajit corpus_patch.lua <list file> <out dir>
-- Each line of the list is "<name>\t<path of the input body>". For every body: one line on stdout
-- "<name>\t<status>\t<reason>\t<ms>"; a patched body is written to <out dir>/<name>.out.
-- The check of the output is done by the Python side, with a different JSON decoder.
package.path = arg[0]:gsub("[^/]*$", "") .. "?.lua;" .. package.path
package.cpath = "/usr/local/openresty/lualib/?.so;" .. package.cpath
local cjson = require("cjson.safe")
cjson.decode_array_with_array_mt(true)   -- as apisix/core/json.lua
local hubfix = require("patch")

local list, out_dir = arg[1], arg[2]
local opts = { decode = cjson.decode, glm_model = "glm-5.3" }
for line in io.lines(list) do
    local name, path = line:match("^([^\t]+)\t(.+)$")
    local f = assert(io.open(path, "rb"))
    local raw = f:read("*a")
    f:close()
    local t0 = os.clock()
    local status, out, reason = hubfix.patch(raw, opts)
    local ms = (os.clock() - t0) * 1000
    if out then
        local o = assert(io.open(out_dir .. "/" .. name .. ".out", "wb"))
        o:write(out)
        o:close()
    end
    io.write(name, "\t", tostring(status), "\t", (reason or ""):gsub("[\t\n]", " "), "\t",
        string.format("%.3f", ms), "\n")
end
