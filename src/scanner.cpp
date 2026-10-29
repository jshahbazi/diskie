#include <sys/attr.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/vnode.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

constexpr uint64_t threshold = 100ULL * 1024 * 1024;
constexpr size_t maxQueued = 2048;
constexpr size_t bulkBufferSize = 64 * 1024;
// Inline work can recurse while a parent's batch is being parsed. A reusable
// buffer per nesting level keeps that parent's records intact.
static thread_local std::vector<std::unique_ptr<char[]>> bulkBuffers;
static thread_local size_t bulkDepth = 0;
struct BulkBuffer {
    char *data;
    BulkBuffer() {
        if (bulkDepth == bulkBuffers.size()) bulkBuffers.emplace_back(new char[bulkBufferSize]);
        data = bulkBuffers[bulkDepth++].get();
    }
    ~BulkBuffer() { --bulkDepth; }
};

struct Node {
    std::string name;
    uint64_t size = 0;
    bool file = false;
    Node *parent = nullptr;
    size_t pending = 1;
    std::vector<std::unique_ptr<Node>> children;
    explicit Node(std::string value, Node *owner = nullptr) : name(std::move(value)), parent(owner) {}
};
struct Job { std::string path; Node *node; unsigned depth; };
struct LinkShard { std::mutex lock; std::unordered_set<uint64_t> inodes; };

// Names are inserted into the chart as text. Replace malformed UTF-8 rather
// than letting a single unusual filename invalidate the entire JSON snapshot.
static void quote(FILE *f, const std::string &s) {
    fputc('"', f);
    for (size_t i = 0; i < s.size();) {
        auto c = static_cast<unsigned char>(s[i]);
        if (c < 0x80) {
            if (c == '"' || c == '\\') { fputc('\\', f); fputc(c, f); }
            else if (c < 32) fprintf(f, "\\u%04x", c);
            else fputc(c, f);
            ++i; continue;
        }
        size_t n = c >= 0xc2 && c <= 0xdf ? 2 : c >= 0xe0 && c <= 0xef ? 3 : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
        bool valid = n && i + n <= s.size();
        for (size_t k = 1; valid && k < n; ++k) valid = (static_cast<unsigned char>(s[i+k]) & 0xc0) == 0x80;
        if (valid && n >= 3) {
            auto b = static_cast<unsigned char>(s[i+1]);
            if ((c == 0xe0 && b < 0xa0) || (c == 0xed && b >= 0xa0) || (c == 0xf0 && b < 0x90) || (c == 0xf4 && b >= 0x90)) valid = false;
        }
        if (valid) { fwrite(s.data()+i, 1, n, f); i += n; }
        else { fputs("\\ufffd", f); ++i; }
    }
    fputc('"', f);
}
static void json(FILE *f, const Node &n) {
    fputs("{\"name\":", f); quote(f, n.name);
    fprintf(f, ",\"size\":%llu", static_cast<unsigned long long>(n.size));
    if (n.file) fputs(",\"file\":true", f);
    else {
        fputs(",\"children\":[", f);
        for (size_t i = 0; i < n.children.size(); ++i) { if (i) fputc(',', f); json(f, *n.children[i]); }
        fputc(']', f);
    }
    fputc('}', f);
}

template<class T> static bool take(const char *&p, const char *end, T &out) {
    if (end-p < static_cast<ptrdiff_t>(sizeof(T))) return false;
    memcpy(&out, p, sizeof(T)); p += sizeof(T); return true;
}

class Scanner {
    dev_t device;
    unsigned workerCount;
    bool useBulk;
    std::unique_ptr<Node> root;
    std::mutex queueLock, treeLock, progressLock;
    std::condition_variable ready, progressWake;
    std::deque<Job> queue;
    size_t jobs = 0;
    bool finished = false;
    std::atomic<bool> reporterDone{false};
    std::array<LinkShard, 64> links;
    std::string currentPath;
    std::atomic<uint64_t> files{0}, directories{0}, errors{0}, mountSkips{0}, bulkCalls{0}, fallbackCalls{0};
    const std::chrono::steady_clock::time_point start = std::chrono::steady_clock::now();

    bool firstLink(uint64_t inode) {
        auto &shard = links[(inode ^ (inode >> 32)) % links.size()];
        std::lock_guard<std::mutex> guard(shard.lock);
        return shard.inodes.insert(inode).second;
    }
    // Each directory contributes one pending unit until its own enumeration is
    // complete. This keeps parents alive while any child is still in flight.
    void finish(Node *n, uint64_t ownSize, std::vector<std::unique_ptr<Node>> largeFiles) {
        std::lock_guard<std::mutex> guard(treeLock);
        n->size += ownSize;
        for (auto &file : largeFiles) n->children.push_back(std::move(file));
        while (--n->pending == 0) {
            std::sort(n->children.begin(), n->children.end(), [](const auto &a, const auto &b) {
                return a->size != b->size ? a->size > b->size : a->name < b->name;
            });
            Node *parent = n->parent;
            if (!parent) break;
            parent->size += n->size;
            if (n->size >= threshold) parent->children.emplace_back(n);
            else delete n;
            n = parent;
        }
    }
    void schedule(const Job &parent, const std::string &name) {
        if (parent.depth >= 512) { ++errors; return; }
        Node *child = new Node(name, parent.node);
        { std::lock_guard<std::mutex> guard(treeLock); ++parent.node->pending; }
        Job job{parent.path + "/" + name, child, parent.depth + 1};
        {
            std::lock_guard<std::mutex> guard(queueLock);
            if (queue.size() < maxQueued) { queue.push_back(std::move(job)); ++jobs; ready.notify_one(); return; }
        }
        // Bound queued paths and memory. Work inline when the queue is full;
        // there is no producer waiting for space, so all workers can progress.
        scan(job);
    }
    void entry(const Job &job, const std::string &name, fsobj_type_t type, uint64_t inode,
               uint32_t nlink, uint64_t allocated, uint64_t &sum, uint64_t &localFiles, std::vector<std::unique_ptr<Node>> &largeFiles) {
        if (name.empty() || name == "." || name == "..") return;
        if (type == VDIR) { schedule(job, name); return; }
        if (type != VREG) return;
        ++localFiles;
        if (nlink > 1 && !firstLink(inode)) return;
        sum += allocated;
        if (allocated >= threshold) {
            auto n = std::make_unique<Node>(name); n->file = true; n->size = allocated; largeFiles.push_back(std::move(n));
        }
    }
    void fallbackEntry(int fd, const Job &job, const std::string &name, uint64_t &sum, uint64_t &localFiles,
                       std::vector<std::unique_ptr<Node>> &largeFiles) {
        struct stat st; ++fallbackCalls;
        if (fstatat(fd, name.c_str(), &st, AT_SYMLINK_NOFOLLOW)) { ++errors; return; }
        if (st.st_dev != device) { ++mountSkips; return; }
        fsobj_type_t type = S_ISDIR(st.st_mode) ? VDIR : S_ISREG(st.st_mode) ? VREG : VLNK;
        entry(job, name, type, st.st_ino, st.st_nlink, static_cast<uint64_t>(std::max<blkcnt_t>(0, st.st_blocks)) * 512, sum, localFiles, largeFiles);
    }
    // Parse only returned attributes; 64-bit attribute fields are 4-byte
    // aligned in this API. memcpy avoids unaligned pointer dereferences.
    bool bulkRecord(int fd, const Job &job, const char *record, uint32_t length, uint64_t &sum, uint64_t &localFiles,
                    std::vector<std::unique_ptr<Node>> &largeFiles) {
        const char *p = record + sizeof(uint32_t), *end = record + length;
        attribute_set_t returned{};
        if (!take(p, end, returned)) return false;
        uint32_t error = 0;
        if (returned.commonattr & ATTR_CMN_ERROR) { if (!take(p, end, error)) return false; }
        if (error) { ++errors; return true; }
        if (!(returned.commonattr & ATTR_CMN_NAME)) return false;
        const char *reference = p; attrreference_t nameRef{};
        if (!take(p, end, nameRef)) return false;
        ptrdiff_t offset = reference - record + nameRef.attr_dataoffset;
        if (offset < 0 || nameRef.attr_length < 1 || static_cast<uint64_t>(offset) + nameRef.attr_length > length) return false;
        const char *name = record + offset;
        if (name[nameRef.attr_length - 1] != '\0') return false;
        std::string value(name, nameRef.attr_length - 1);
        fsobj_type_t type = VNON; uint64_t inode = 0; uint32_t nlink = 0; off_t allocation = 0;
        if ((returned.commonattr & ATTR_CMN_OBJTYPE) && !take(p, end, type)) return false;
        if ((returned.commonattr & ATTR_CMN_FILEID) && !take(p, end, inode)) return false;
        if ((returned.fileattr & ATTR_FILE_LINKCOUNT) && !take(p, end, nlink)) return false;
        if ((returned.fileattr & ATTR_FILE_ALLOCSIZE) && !take(p, end, allocation)) return false;
        bool common = (returned.commonattr & (ATTR_CMN_OBJTYPE | ATTR_CMN_FILEID)) == (ATTR_CMN_OBJTYPE | ATTR_CMN_FILEID);
        bool file = (returned.fileattr & (ATTR_FILE_LINKCOUNT | ATTR_FILE_ALLOCSIZE)) == (ATTR_FILE_LINKCOUNT | ATTR_FILE_ALLOCSIZE);
        if (!common || (type == VREG && !file)) fallbackEntry(fd, job, value, sum, localFiles, largeFiles);
        else entry(job, value, type, inode, nlink, static_cast<uint64_t>(std::max<off_t>(0, allocation)), sum, localFiles, largeFiles);
        return true;
    }
    void scan(const Job &job) {
        { std::lock_guard<std::mutex> guard(progressLock); currentPath = job.path; }
        int fd = open(job.path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        uint64_t sum = 0, localFiles = 0, localBulkCalls = 0;
        std::vector<std::unique_ptr<Node>> largeFiles;
        if (fd < 0) { ++errors; finish(job.node, 0, {}); return; }
        struct stat st;
        if (fstat(fd, &st)) { ++errors; close(fd); finish(job.node, 0, {}); return; }
        if (st.st_dev != device) { ++mountSkips; close(fd); finish(job.node, 0, {}); return; }
        ++directories;
        bool fallback = !useBulk;
        if (useBulk) {
            struct attrlist attrs{}; attrs.bitmapcount = ATTR_BIT_MAP_COUNT;
            attrs.commonattr = ATTR_CMN_RETURNED_ATTRS | ATTR_CMN_ERROR | ATTR_CMN_NAME | ATTR_CMN_OBJTYPE | ATTR_CMN_FILEID;
            attrs.fileattr = ATTR_FILE_LINKCOUNT | ATTR_FILE_ALLOCSIZE;
            BulkBuffer buffer;
            bool first = true;
            for (;;) {
                ++localBulkCalls;
                int count = getattrlistbulk(fd, &attrs, buffer.data, bulkBufferSize, 0);
                if (count < 0) {
                    if (errno == EINTR) continue;
                    if (first && (errno == ENOTSUP || errno == EINVAL || errno == ENOSYS)) fallback = true;
                    else ++errors;
                    break;
                }
                if (count == 0) break;
                first = false;
                const char *p = buffer.data, *end = p + bulkBufferSize;
                for (int i = 0; i < count; ++i) {
                    uint32_t length = 0;
                    if (end-p < 4) { ++errors; break; }
                    memcpy(&length, p, 4);
                    if (length < sizeof(uint32_t) + sizeof(attribute_set_t) || static_cast<uint64_t>(end-p) < length) { ++errors; break; }
                    if (!bulkRecord(fd, job, p, length, sum, localFiles, largeFiles)) ++errors;
                    p += length;
                }
            }
        }
        if (fallback) {
            // A fresh descriptor is required: bulk and readdir offsets must not
            // be mixed, including when a filesystem rejects the bulk API.
            close(fd); fd = open(job.path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            DIR *dir = fd >= 0 ? fdopendir(fd) : nullptr;
            if (!dir) { ++errors; if (fd >= 0) close(fd); }
            else {
                for (;;) {
                    errno = 0; struct dirent *e = readdir(dir);
                    if (!e) { if (errno) ++errors; break; }
                    if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
                    fallbackEntry(dirfd(dir), job, e->d_name, sum, localFiles, largeFiles);
                }
                closedir(dir);
            }
        } else close(fd);
        // Publish once per directory instead of bouncing a shared cache line
        // for every regular file and metadata syscall.
        if (localFiles) files.fetch_add(localFiles, std::memory_order_relaxed);
        if (localBulkCalls) bulkCalls.fetch_add(localBulkCalls, std::memory_order_relaxed);
        finish(job.node, sum, std::move(largeFiles));
    }
    void worker() {
        for (;;) {
            Job job;
            {
                std::unique_lock<std::mutex> guard(queueLock);
                ready.wait(guard, [&]{ return finished || !queue.empty(); });
                if (queue.empty()) return;
                job = std::move(queue.front()); queue.pop_front();
            }
            scan(job);
            {
                std::lock_guard<std::mutex> guard(queueLock);
                if (--jobs == 0) { finished = true; ready.notify_all(); }
            }
        }
    }
    void progress(const std::string &path) {
        while (!reporterDone) {
            std::string current;
            { std::lock_guard<std::mutex> guard(progressLock); current = currentPath; }
            std::string temp = path + ".new";
            if (FILE *f = fopen(temp.c_str(), "w")) {
                fprintf(f, "{\"files\":%llu,\"directories\":%llu,\"workers\":%u,\"seconds\":%.1f,\"path\":",
                        static_cast<unsigned long long>(files.load()), static_cast<unsigned long long>(directories.load()), workerCount, elapsed());
                quote(f, current); fputs("}", f);
                if (!fclose(f)) rename(temp.c_str(), path.c_str());
            }
            std::unique_lock<std::mutex> guard(progressLock);
            progressWake.wait_for(guard, std::chrono::milliseconds(750), [&]{ return reporterDone.load(); });
        }
    }
public:
    Scanner(std::string path, dev_t dev, unsigned workers, bool bulk) : device(dev), workerCount(workers), useBulk(bulk), root(std::make_unique<Node>("Data volume")) {
        queue.push_back(Job{std::move(path), root.get(), 0}); jobs = 1;
    }
    double elapsed() const { return std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count(); }
    void run(const std::string &progressPath) {
        std::thread reporter([&]{ progress(progressPath); });
        std::vector<std::thread> workers;
        for (unsigned i = 0; i < workerCount; ++i) workers.emplace_back([&]{ worker(); });
        for (auto &thread : workers) thread.join();
        reporterDone = true; progressWake.notify_one(); reporter.join();
        unlink(progressPath.c_str()); unlink((progressPath + ".new").c_str());
    }
    bool save(const std::string &output, const struct statfs &fs) {
        FILE *f = fopen(output.c_str(), "w"); if (!f) return false;
        auto t = time(nullptr); struct tm local{}; localtime_r(&t, &local); char date[80]; strftime(date, sizeof(date), "%Y-%m-%d %H:%M %Z", &local);
        fputs("{\"tree\":", f); json(f, *root);
        fprintf(f, ",\"total\":%llu,\"free\":%llu,\"errors\":{\"unreadable\":%llu},\"files\":%llu,\"directories\":%llu,\"workers\":%u,\"seconds\":%.3f,\"bulkCalls\":%llu,\"fallbackCalls\":%llu,\"mountsSkipped\":%llu,\"date\":",
                static_cast<unsigned long long>(fs.f_blocks)*fs.f_bsize, static_cast<unsigned long long>(fs.f_bavail)*fs.f_bsize,
                static_cast<unsigned long long>(errors.load()), static_cast<unsigned long long>(files.load()), static_cast<unsigned long long>(directories.load()), workerCount, elapsed(),
                static_cast<unsigned long long>(bulkCalls.load()), static_cast<unsigned long long>(fallbackCalls.load()), static_cast<unsigned long long>(mountSkips.load()));
        quote(f, date); fputs("}", f); return fclose(f) == 0;
    }
};
int main(int argc, char **argv) {
    if (argc < 2 || argc > 5) { fprintf(stderr, "usage: disk-scanner OUTPUT [ROOT [WORKERS [posix]]]\n"); return 2; }
    const std::string path = argc >= 3 ? argv[2] : "/System/Volumes/Data";
    unsigned workers = argc >= 4 ? static_cast<unsigned>(atoi(argv[3])) : std::min(6u, std::max(1u, std::thread::hardware_concurrency()/2));
    if (workers < 1 || workers > 16) return 2;
    struct stat st; struct statfs fs;
    if (lstat(path.c_str(), &st) || !S_ISDIR(st.st_mode) || statfs(path.c_str(), &fs)) return 2;
    try {
        Scanner scanner(path, st.st_dev, workers, !(argc == 5 && !strcmp(argv[4], "posix")));
        scanner.run(std::string(argv[1]) + ".progress");
        if (statfs(path.c_str(), &fs)) return 2;
        return scanner.save(argv[1], fs) ? 0 : 2;
    } catch (const std::exception &e) { fprintf(stderr, "scan failed: %s\n", e.what()); return 2; }
}
