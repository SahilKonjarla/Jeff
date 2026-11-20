/* Freestanding JFG2 PID1 SOURCE. Build/ELF inspection is not Linux behavior proof.
 * Invocation: guard --jfg2 DIGEST STARTUP_NS RUNTIME_NS RATE_PPM MARGIN_NS -- /exec args...
 * Raw stdout after challenge; PID1 alone owns framed stderr. Never linked to Jeff here.
 */
#include "guard_v2_abi.h"

__attribute__((used,section(".jeff.guard")))
const g_u8 jeff_guard_v2_abi[]={'J','G','O','2',77,8,2,G_ABI,5,21,53,61,69,1,0,0};

struct g2_context {
    struct g_guard life;
    g_u64 sequence, primary_observed;
    g_u8 pending[G2_FRAME];
    unsigned length, offset, kind;
    int stderr_read, stderr_eof, dropped, primary_done, primary_status, children_gone;
    struct g2_failure *failure;
};

__attribute__((noreturn)) static void g2_exit(unsigned code) {
    G_CALL(exit_group,code,0,0,0,0,0);
    for (;;) __builtin_trap();
}

__attribute__((naked,noreturn)) static void g2_restore(void) {
#if defined(__aarch64__)
    __asm__ volatile("mov x8, #139\nsvc #0\nbrk #0");
#else
    __asm__ volatile("mov $15, %rax\nsyscall\nud2");
#endif
}

__attribute__((noreturn)) static void g2_term(int signal) {
    (void)signal;
    g2_exit(125); /* No asynchronous/reentrant framing; missing terminal stays unknown. */
}

static g_u64 g2_now(void) {
    struct g_timespec t;
    if (G_CALL(clock_gettime,7,&t,0,0,0,0)<0 || t.seconds<0 || t.nanos<0 ||
        t.nanos>=1000000000 || (g_u64)t.seconds>(G_MAX_NS-(g_u64)t.nanos)/1000000000)
        g2_exit(126); /* Clock unreadable: never manufacture a terminal timestamp. */
    return (g_u64)t.seconds*1000000000+(g_u64)t.nanos;
}

/* At most one nonblocking write. Sequence is committed only for a COMPLETE frame. */
static void g2_flush(struct g2_context *c) {
    if (!c->length) return;
    long n=G_CALL(write,2,c->pending+c->offset,c->length-c->offset,0,0,0);
    if (n>0 && (g_u64)n<=c->length-c->offset) {
        c->offset+=(unsigned)n;
        if (c->offset==c->length) {
            if (c->sequence==G_MAX_NS) g2_exit(125);
            ++c->sequence;
            c->length=c->offset=0;
        }
    } else if (n!=-4 && n!=-11) g2_exit(125);
}

static void g2_primary_reason(struct g2_context *c, unsigned *cause,
                              unsigned *detail, unsigned *code) {
    unsigned signal=(unsigned)c->primary_status&127;
    *code=signal ? 128+signal : ((unsigned)c->primary_status>>8)&255;
    g_u32 failure=__atomic_load_n(&c->failure->code,__ATOMIC_ACQUIRE);
    if (failure || signal>64) {
        *cause=4; *detail=failure==2 ? 3 : 5; /* exec_failed or internal_error */
    } else {
        *cause=signal ? 2 : 1;
        *detail=signal ? signal : *code;
    }
}

__attribute__((noreturn)) static void g2_finish(struct g2_context *c, unsigned cause,
                                               unsigned detail, g_u64 observed, unsigned code) {
    /* Never splice terminal bytes into an incomplete payload. A whole UNSENT
     * frame may be discarded; its sequence was never committed. No time is renewed.
     */
    if (c->length) g2_flush(c);
    if (c->length) {
        if (c->offset) g2_exit(code);
        if (c->kind==5) c->dropped=1;
        if (c->kind==4 && (cause==1 || cause==2 || (cause==4 && detail==3))) g2_exit(code);
        c->length=0;
    }
    if (c->sequence==G_MAX_NS) g2_exit(code);
    g2_terminal(&c->life,c->pending,c->sequence+1,observed,cause,detail,
                G2_TERM_FLAGS(c->stderr_eof,c->dropped));
    c->length=81; c->offset=0; c->kind=6;
    /* Finite best effort, including partial-write/EINTR cases. No poll/drain wait. */
    for (unsigned i=0;i<4 && c->length;++i) g2_flush(c);
    g2_exit(code); /* PID1 exit tears down descendants; no terminal delivery guarantee. */
}

static void g2_tick(struct g2_context *c) {
    g_u64 now=g2_now();
    unsigned decision=G2_CLOCK_DECISION(now,c->life.last,c->life.deadline);
    if (decision==1) g2_finish(c,4,1,now,126); /* Explicit clock fault, NOT timeout. */
    if (decision==2) {
        if (c->life.state!=1) g2_finish(c,4,2,now,125);
        if (c->primary_done) {
            unsigned cause, detail, code;
            g2_primary_reason(c,&cause,&detail,&code);
            g2_finish(c,cause,detail,c->primary_observed,code);
        }
        g2_finish(c,3,0,now,124);
    }
    c->life.last=now;
}

static void g2_pause(struct g2_context *c, int fd, g_u16 events) {
    g2_tick(c);
    g_u64 left=c->life.deadline-c->life.last;
    struct g_timespec timeout={0,(g_i64)(left<100000000 ? left : 100000000)};
    struct g_pollfd descriptors[2]={{0,1,0},{fd,events,0}};
    long n=G_CALL(ppoll,descriptors,2,&timeout,0,8,0);
    if (n<0 && n!=-4) g2_finish(c,4,5,c->life.last,125);
}

static void g2_startup_checks(void) {
    g_u32 ids[3];
    struct { g_u32 version; int pid; } header={0x20080522U,0};
    struct { g_u32 effective,permitted,inheritable; } caps[2];
    if (G_CALL(getpid,0,0,0,0,0,0)!=1 ||
        G_CALL(getresuid,&ids[0],&ids[1],&ids[2],0,0,0)<0 ||
        ids[0]!=65532 || ids[1]!=65532 || ids[2]!=65532) g2_exit(126);
    if (G_CALL(getresgid,&ids[0],&ids[1],&ids[2],0,0,0)<0 ||
        ids[0]!=65532 || ids[1]!=65532 || ids[2]!=65532) g2_exit(126);
    if (G_CALL(capget,&header,caps,0,0,0,0)<0 || caps[0].effective || caps[0].permitted ||
        caps[0].inheritable || caps[1].effective || caps[1].permitted || caps[1].inheritable ||
        G_CALL(prctl,39,0,0,0,0,0)!=1 || G_CALL(prctl,21,0,0,0,0,0)!=2 ||
        G_CALL(prctl,4,0,0,0,0,0)<0 || G_CALL(prctl,3,0,0,0,0,0)!=0) g2_exit(126);
    long groups=G_CALL(getgroups,0,0,0,0,0,0);
    if (groups<0 || groups>1 || (groups==1 &&
        (G_CALL(getgroups,1,&ids[0],0,0,0,0)!=1 || ids[0]!=65532))) g2_exit(126);
    if (G_CALL(close_range,3,0xffffffffU,0,0,0,0)<0) g2_exit(126);
    struct g_action term={g2_term,0x04000000UL,g2_restore,0}, defaults={0,0,0,0};
    g_u64 mask=0;
    if (G_CALL(rt_sigaction,15,&term,0,8,0,0)<0 ||
        G_CALL(rt_sigaction,17,&defaults,0,8,0,0)<0 ||
        G_CALL(rt_sigprocmask,2,&mask,0,8,0,0)<0) g2_exit(126);
}

static int g2_literal(const char *a, const char *b) {
    while (*b) if (*a++!=*b++) return 0;
    return !*a;
}

static int g2_arguments(g_u64 argc, char **argv) {
    if (argc<9 || argc>72 || !g2_literal(argv[1],"--jfg2") ||
        !g2_literal(argv[7],"--") || argv[8][0]!='/' || argv[8][1]=='/') return 0;
    unsigned total=0;
    for (g_u64 i=8;i<argc;++i) {
        unsigned size=0;
        while (argv[i][size]) {
            unsigned char ch=(unsigned char)argv[i][size];
            if (ch<32 || ch==127 || ++size>4096 || ++total>8192) return 0;
        }
    }
    const char *path=argv[8];
    for (unsigned i=0;path[i];++i)
        if (path[i]=='/' && path[i+1]=='.' && path[i+2]=='.' &&
            (!path[i+3] || path[i+3]=='/')) return 0;
    return 1;
}

static long g2_flags(int fd, unsigned access) {
    long flags=G_CALL(fcntl,fd,3,0,0,0,0);
    if (!G_IO_ACCESS_OK(flags,access)) g2_exit(126);
    return flags;
}

static void g2_nonblocking(int fd, long flags) {
    if (G_CALL(fcntl,fd,4,flags|0x800,0,0,0)<0) g2_exit(126);
}

static g_u64 g2_pipe_identity(unsigned fd) {
    char path[]="/proc/self/fd/0", link[64];
    path[14]=(char)('0'+fd);
    long n=G_CALL(readlinkat,-100,path,link,sizeof(link),0,0);
    if (n<=0 || n>=64) g2_exit(126);
    g_u64 id=g2_pipe_id(link,(unsigned)n);
    if (!id) g2_exit(126);
    return id;
}

static long g2_stdio(void) {
    long input=g2_flags(0,0), output=g2_flags(1,1), status=g2_flags(2,1);
    g_u64 a=g2_pipe_identity(0), b=g2_pipe_identity(1), c=g2_pipe_identity(2);
    if (!G2_PIPE_IDS_OK(a,b,c)) g2_exit(126);
    /* All identity/access checks precede F_SETFL/challenge/fork. */
    g2_nonblocking(0,input); g2_nonblocking(1,output); g2_nonblocking(2,status);
    return output;
}

static void g2_challenge(struct g2_context *c) {
    g_u8 bytes[77]; unsigned offset=0;
    g2_header(&c->life,bytes,1,c->life.issued,c->life.startup,c->life.runtime);
    while (offset<77) {
        g2_tick(c);
        long n=G_CALL(write,1,bytes+offset,77-offset,0,0,0);
        if (n>0 && (g_u64)n<=77-offset) offset+=(unsigned)n;
        else if (n==-11) g2_pause(c,1,4);
        else if (n!=-4) g2_exit(125);
    }
}

static void g2_authorization(struct g2_context *c) {
    g_u8 bytes[77]; unsigned offset=0;
    while (offset<77) {
        g2_tick(c);
        long n=G_CALL(read,0,bytes+offset,77-offset,0,0,0);
        if (n>0 && (g_u64)n<=77-offset) offset+=(unsigned)n;
        else if (n==-11) g2_pause(c,0,1);
        else if (n!=-4) g2_finish(c,4,4,c->life.last,125);
    }
    g2_tick(c);
    if (!g2_arm(&c->life,bytes,c->life.last)) g2_finish(c,4,2,c->life.last,125);
}

static void g2_control_before_clone(struct g2_context *c) {
    g2_tick(c);
    g_u8 extra;
    long control=G_CALL(read,0,&extra,1,0,0,0);
    if (!G2_CONTROL_IDLE(control)) g2_finish(c,4,4,c->life.last,125);
    /* Only a point-in-time observation: owner loss can still race the next clone. */
}

__attribute__((noreturn)) static void g2_child_failure(struct g2_failure *shared, unsigned code) {
    /* No potentially failing error-pipe write or reserved workload exit/signal.
     * This preallocated shared page disappears from the child on successful exec.
     */
    __atomic_store_n(&shared->code,code,__ATOMIC_RELEASE);
    g2_exit(code==2 ? 127 : 126);
}

__attribute__((noreturn)) static void g2_child(char **argv, char **environment,
                                             int stderr_write, struct g2_failure *shared) {
    struct g_action defaults={0,0,0,0};
    if (G_CALL(rt_sigaction,15,&defaults,0,8,0,0)<0) g2_child_failure(shared,1);
    long input=G_CALL(openat,-100,"/dev/null",0x80000,0,0,0);
    if (input<3 || G_CALL(dup3,input,0,0,0,0,0)<0 ||
        G_CALL(dup3,stderr_write,2,0,0,0,0)<0 ||
        G_CALL(close_range,3,0xffffffffU,0,0,0,0)<0) g2_child_failure(shared,1);
    struct g_program policy={(g_u16)(sizeof(g_child_policy)/sizeof(g_child_policy[0])),g_child_policy};
    if (G_CALL(prctl,38,1,0,0,0,0)<0 || G_CALL(seccomp,1,0,&policy,0,0,0)<0)
        g2_child_failure(shared,1);
    G_CALL(execve,argv[0],argv,environment,0,0,0);
    g2_child_failure(shared,2);
}

static void g2_reap(struct g2_context *c, long primary) {
    if (c->children_gone) return;
    for (unsigned i=0;i<64;++i) {
        g2_tick(c);
        int status=0;
        long child=G_CALL(wait4,-1,&status,G2_WAIT_ALL,0,0,0);
        if (child==primary && !c->primary_done) {
            g2_tick(c); /* Deadline wins if normal reap is not observed before it. */
            c->primary_done=1; c->primary_status=status; c->primary_observed=c->life.last;
            continue;
        }
        if (child==0 || child==-4) return;
        if (child==-10 && c->primary_done) { c->children_gone=1; return; }
        if (child<0) g2_finish(c,4,5,c->life.last,125);
    }
}

__attribute__((noreturn)) static void g2_watch(struct g2_context *c, long primary) {
    for (;;) {
        g2_tick(c);
        g_u8 extra;
        long control=G_CALL(read,0,&extra,1,0,0,0);
        if (control>=0 || (control!=-4 && control!=-11)) g2_finish(c,4,4,c->life.last,125);
        g2_reap(c,primary);
        g2_flush(c);
        int unavailable=0;
        if (!c->length && !c->stderr_eof) {
            g2_tick(c);
            long n=G_CALL(read,c->stderr_read,c->pending+77,G2_DATA,0,0,0);
            if (n>0 && n<=G2_DATA) {
                if (c->sequence==G_MAX_NS) g2_exit(125);
                g2_header(&c->life,c->pending,5,c->sequence+1,(g_u64)n,0);
                c->length=77+(unsigned)n; c->offset=0; c->kind=5;
            } else if (n==0) {
                c->stderr_eof=G2_FINAL_STDERR_EOF(1,c->children_gone);
                unavailable=!c->stderr_eof;
            } else if (n==-11) unavailable=1;
            else if (n!=-4) g2_finish(c,4,5,c->life.last,125);
        }
        if (c->primary_done && !c->length && (c->stderr_eof || unavailable)) {
            unsigned cause,detail,code;
            g2_primary_reason(c,&cause,&detail,&code);
            g2_finish(c,cause,detail,c->primary_observed,code);
        }
        if (c->length) g2_pause(c,2,4);
        else g2_pause(c,(c->stderr_eof || unavailable) ? 0 : c->stderr_read,1);
    }
}

__attribute__((used,visibility("hidden"),noreturn))
void guard_v2_main(g_u64 *stack) {
    g_u64 argc=stack[0];
    if (argc<9 || argc>72) g2_exit(126);
    char **argv=(char **)(stack+1), **environment=argv+argc+1;
    if (!g2_arguments(argc,argv)) g2_exit(126);
    g2_startup_checks();
    long output_flags=g2_stdio();
    struct g2_context c;
    c.sequence=c.primary_observed=0;
    c.length=c.offset=c.kind=0;
    c.stderr_read=-1; c.stderr_eof=c.dropped=c.primary_done=c.primary_status=c.children_gone=0;
    c.failure=(struct g2_failure *)0;
    g_u64 startup,runtime,rate,margin;
    if (!g_hex_digest(argv[2],c.life.digest) || !g_decimal(argv[3],&startup) ||
        !g_decimal(argv[4],&runtime) || !g_decimal(argv[5],&rate) ||
        !g_decimal(argv[6],&margin) || G_CALL(getrandom,c.life.nonce,16,1,0,0,0)!=16 ||
        !g_initialize(&c.life,g2_now(),startup,runtime,rate,margin)) g2_exit(126);
    g2_challenge(&c); g2_authorization(&c); g2_tick(&c);
    if (G_CALL(fcntl,1,4,output_flags,0,0,0)<0) g2_finish(&c,4,2,c.life.last,126);
    int pipes[2];
    if (G_CALL(pipe2,pipes,0x80000,0,0,0,0)<0 || pipes[0]<3 || pipes[1]<3)
        g2_finish(&c,4,2,c.life.last,126);
    c.stderr_read=pipes[0];
    g2_nonblocking(pipes[0],g2_flags(pipes[0],0));
    long address=G_CALL(mmap,0,4096,3,0x21,-1,0); /* RW, SHARED|ANONYMOUS; never executable. */
    if (address<=0) g2_finish(&c,4,2,c.life.last,126);
    c.failure=(struct g2_failure *)address;
    __atomic_store_n(&c.failure->code,0,__ATOMIC_RELEASE); /* Prefault BEFORE child exists. */
    g2_control_before_clone(&c);
    long child=G_CALL(clone,17,0,0,0,0,0); /* SIGCHLD, separate VM; no vfork/CLONE_VM. */
    if (child<0) g2_finish(&c,4,2,c.life.last,126);
    if (!child) g2_child(argv+8,environment,pipes[1],c.failure);
    if (G_CALL(close,pipes[1],0,0,0,0,0)<0) g2_finish(&c,4,5,c.life.last,125);
    g2_tick(&c);
    g2_header(&c.life,c.pending,4,1,c.life.last,0);
    c.length=77; c.offset=0; c.kind=4;
    g2_watch(&c,child);
}

__attribute__((naked,noreturn)) void _start(void) {
#if defined(__aarch64__)
    __asm__ volatile("mov x0, sp\nbl guard_v2_main\nbrk #0");
#else
    __asm__ volatile("mov %rsp, %rdi\nand $-16, %rsp\ncall guard_v2_main\nud2");
#endif
}
