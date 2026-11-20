/* JFG2 wire/core SOURCE. Reuses frozen JFG1 scalar/budget primitives only. */
#ifndef JEFF_GUARD_V2_CORE_H
#define JEFF_GUARD_V2_CORE_H
#include "guard_core.h"
#define G2_DATA 4096U
#define G2_FRAME (G_WIRE+G2_DATA)
/* A pre-dispatch read must positively observe EAGAIN. EINTR is not liveness. */
#define G2_CONTROL_IDLE(result) ((result)==-11)
#define G2_PIPE_IDS_OK(a,b,c) ((a)>0 && (b)>0 && (c)>0 && (a)!=(b) && (a)!=(c) && (b)!=(c))
#define G2_CLOCK_DECISION(now,last,deadline) \
    (((now)>G_MAX_NS || (now)<(last)) ? 1 : ((now)>=(deadline) ? 2 : 0))
/* A transient pipe EOF is not final while a job process could reopen a writer. */
#define G2_FINAL_STDERR_EOF(read_zero,children_gone) ((read_zero) && (children_gone))
#define G2_WAIT_ALL 0x40000001U /* __WALL|WNOHANG: include non-SIGCHLD clone children. */
#define G2_TERM_FLAGS(eof,dropped) ((unsigned)(!!(eof))|((unsigned)(!!(dropped))<<1))

static inline void g2_header(const struct g_guard *g, g_u8 *p, unsigned operation,
                             g_u64 first, g_u64 second, g_u64 third) {
    p[0]='J'; p[1]='F'; p[2]='G'; p[3]='2'; p[4]=(g_u8)operation;
    for (unsigned i=0;i<16;++i) p[G_NONCE+i]=g->nonce[i];
    for (unsigned i=0;i<32;++i) p[G_DIGEST+i]=g->digest[i];
    g_put64(p+G_WORDS,first); g_put64(p+G_WORDS+8,second); g_put64(p+G_WORDS+16,third);
}

static inline void g2_terminal(const struct g_guard *g, g_u8 *p, g_u64 sequence,
                               g_u64 observed, unsigned cause, unsigned detail, unsigned flags) {
    g2_header(g,p,6,sequence,observed,4);
    p[77]=(g_u8)cause; p[78]=(g_u8)flags;
    p[79]=(g_u8)(detail>>8); p[80]=(g_u8)detail;
}

static inline int g2_arm(struct g_guard *g, const g_u8 *p, g_u64 now) {
    if (!g_tick(g,now)) return 0;
    if (g->state!=0 || p[0]!='J' || p[1]!='F' || p[2]!='G' || p[3]!='2' || p[4]!=2 ||
        !g_equal(p+G_NONCE,g->nonce,16) || !g_equal(p+G_DIGEST,g->digest,32) ||
        g_get64(p+G_WORDS+8) || g_get64(p+G_WORDS+16)) { g->state=2; return 0; }
    g_u64 deadline=g_get64(p+G_WORDS);
    if (deadline<=now || deadline>g->maximum) { g->state=2; return 0; }
    g->deadline=deadline; g->state=1; return 1;
}

/* Linux procfs anonymous-pipe magic-link identity, not descriptor-number equality.
 * Enforces canonical pipe:[inode], fails closed on sockets/files/TTY/truncation.
 * Requires trusted procfs and stable startup descriptors; runtime still unproved.
 */
static inline g_u64 g2_pipe_id(char *text, unsigned length) {
    const char *prefix="pipe:[";
    if (length<8 || length>=64 || text[length-1]!=']') return 0;
    for (unsigned i=0;i<6;++i) if (text[i]!=prefix[i]) return 0;
    text[length-1]=0;
    g_u64 result;
    return g_decimal(text+6,&result) && result ? result : 0;
}
#endif
