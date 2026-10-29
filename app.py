from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

# Assignment 2.1: whole-word, case-insensitive pattern matching any word/phrase in the list
def word_pattern(words):
    alternatives = '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf'(?<!\w)(?:{alternatives})(?!\w)', re.IGNORECASE)

TIER1_RE = word_pattern(TIER1_WORDS)
TIER2_RE = word_pattern(TIER2_PHRASES)
TIER3_RE = word_pattern(TIER3_WORDS)
URL_RE = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

# Creates the tables/columns added for the Design Claim features if they don't exist yet
def init_db():
    db = sqlite3.connect(DATABASE)
    # Design Claim 4: one row = "requester asked target to reply to post"
    db.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            seen INTEGER DEFAULT 0 NOT NULL,
            answered_at TIMESTAMP,
            UNIQUE (post_id, target_id)
        )
    ''')
    # Design Claim 23: the post a badge holder has pinned to their profile
    user_columns = [row[1] for row in db.execute('PRAGMA table_info(users)')]
    if 'pinned_post_id' not in user_columns:
        db.execute('ALTER TABLE users ADD COLUMN pinned_post_id INTEGER')
    db.commit()
    db.close()

init_db()

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

# Design Claim 4: unread request count for the nav bar, available on every page
@app.context_processor
def inject_request_count():
    user_id = session.get('user_id')
    if not user_id:
        return {}
    row = query_db('SELECT COUNT(*) AS cnt FROM requests WHERE target_id = ? AND seen = 0 AND answered_at IS NULL',
                   (user_id,), one=True)
    return {'unread_requests': row['cnt'] if row else 0}

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())

# Design Claim 12: people the current user follows who commented on this post
def followed_commenters(user_id, post_id):
    if not user_id:
        return []
    rows = query_db('''
        SELECT DISTINCT u.username FROM comments c
        JOIN follows f ON f.followed_id = c.user_id AND f.follower_id = ?
        JOIN users u ON u.id = c.user_id
        WHERE c.post_id = ?
    ''', (user_id, post_id))
    return [r['username'] for r in rows]

# Design Claim 23: badges are calculated from activity, rewarding quality over quantity
def user_badges(user_id):
    stats = query_db('''
        SELECT
            (SELECT COUNT(*) FROM requests
             WHERE target_id = :id AND answered_at IS NOT NULL) AS answered,
            (SELECT COUNT(*) FROM reactions r JOIN posts p ON r.post_id = p.id
             WHERE p.user_id = :id) AS reactions,
            (SELECT MAX(cnt) FROM (
                SELECT COUNT(*) AS cnt FROM comments c JOIN posts p ON c.post_id = p.id
                WHERE p.user_id = :id AND c.user_id != :id GROUP BY c.post_id)) AS top_comments
    ''', {'id': user_id}, one=True)

    badges = []
    if stats['answered'] >= 3:
        badges.append({'icon': '🤝', 'name': 'Helper', 'why': 'Answered 3 requests from other members'})
    if (stats['top_comments'] or 0) >= 10:
        badges.append({'icon': '💬', 'name': 'Conversation Starter', 'why': 'Wrote a post that got 10+ comments'})
    if stats['reactions'] >= 50:
        badges.append({'icon': '⭐', 'name': 'Well Liked', 'why': 'Received 50+ reactions on posts'})
    return badges


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        post_dict['badges'] = user_badges(post['user_id'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated,
            'followed_commenters': followed_commenters(current_user_id, post['id'])
        })

    #  4. Render Template with Pagination Info 
    # Design Claim 18 (Ch. 5): members who joined in the last 7 days, their newest post, and whether you already greeted them
    new_members = []
    if current_user_id:
        new_members = query_db('''
            SELECT u.id, u.username, newest.post_id,
                   EXISTS (SELECT 1 FROM comments c WHERE c.user_id = ? AND c.post_id = newest.post_id) AS welcomed
            FROM users u
            LEFT JOIN (SELECT user_id, MAX(id) AS post_id FROM posts GROUP BY user_id) newest ON newest.user_id = u.id
            WHERE u.created_at >= datetime('now', '-7 days') AND u.id != ?
            ORDER BY u.created_at DESC LIMIT 5
        ''', (current_user_id, current_user_id))

    return render_template('feed.html.j2',
                           posts=posts_data,
                           new_members=new_members,
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Design Claim 20 (Ch. 4): show a reminder instead of saving if rules would be broken
    reminder = rule_reminder(content or '', url_for('add_post'))
    if reminder:
        return reminder

    # Original text is stored; moderation is applied when it's displayed
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

# Design Claim 23: privilege for badge holders, pin or unpin one own post
@app.route('/posts/<int:post_id>/pin', methods=['POST'])
def pin_post(post_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to pin a post.', 'danger')
        return redirect(url_for('login'))

    post = query_db('SELECT user_id FROM posts WHERE id = ?', (post_id,), one=True)
    if not post or post['user_id'] != user_id:
        flash('You can only pin your own posts.', 'danger')
        return redirect(request.referrer or url_for('feed'))
    if not user_badges(user_id):
        flash('Earn a badge to unlock pinning posts.', 'warning')
        return redirect(request.referrer or url_for('feed'))

    current = query_db('SELECT pinned_post_id FROM users WHERE id = ?', (user_id,), one=True)
    new_pin = None if current['pinned_post_id'] == post_id else post_id
    query_db('UPDATE users SET pinned_post_id = ? WHERE id = ?', (new_pin, user_id), commit=True)
    flash('Post pinned to your profile.' if new_pin else 'Post unpinned.', 'success')
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        post['pinned'] = post['id'] == user['pinned_post_id']
        posts.append(post)
    posts.sort(key=lambda p: not p['pinned'])  # pinned post first

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           badges=user_badges(user['id']),
                           # Design Claim 23: progress toward the Helper badge
                           helper_progress=query_db('SELECT COUNT(*) AS cnt FROM requests WHERE target_id = ? AND answered_at IS NOT NULL',
                                                    (user['id'],), one=True)['cnt'])
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Design Claim 4: suggest followed users who haven't commented or been asked yet
    suggestions = []
    current_user_id = session.get('user_id')
    if current_user_id:
        suggestions = query_db('''
            SELECT u.username FROM follows f
            JOIN users u ON f.followed_id = u.id
            WHERE f.follower_id = ? AND u.id != ?
              AND u.id NOT IN (SELECT user_id FROM comments WHERE post_id = ?)
              AND u.id NOT IN (SELECT target_id FROM requests WHERE post_id = ?)
            ORDER BY RANDOM() LIMIT 5
        ''', (current_user_id, post['user_id'], post_id, post_id))

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           suggestions=suggestions,
                           followed_names=followed_commenters(current_user_id, post_id),
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('welcome')) # Design Claim 19: invite the newcomer to introduce themselves

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Design Claim 20 (Ch. 4): show a reminder instead of saving if rules would be broken
    reminder = rule_reminder(content or '', url_for('add_comment', post_id=post_id))
    if reminder:
        return reminder

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        # Design Claim 4: replying to the post answers any request for it
        db.execute('''UPDATE requests SET answered_at = CURRENT_TIMESTAMP
                      WHERE post_id = ? AND target_id = ? AND answered_at IS NULL''',
                   (post_id, user_id))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.form.get('back') or request.referrer or url_for('post_detail', post_id=post_id))

# Design Claim 4: ask a specific user to reply to a post
@app.route('/posts/<int:post_id>/ask', methods=['POST'])
def ask_user(post_id):
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to ask someone.', 'danger')
        return redirect(url_for('login'))

    back = request.referrer or url_for('post_detail', post_id=post_id)

    if not query_db('SELECT 1 FROM posts WHERE id = ?', (post_id,), one=True):
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    username = request.form.get('username', '').strip().lstrip('@')
    target = query_db('SELECT id, username FROM users WHERE username = ?', (username,), one=True)
    if not target:
        flash(f'User "{username}" does not exist.', 'warning')
        return redirect(back)
    if target['id'] == user_id:
        flash('You cannot ask yourself.', 'warning')
        return redirect(back)

    db = get_db()
    try:
        db.execute('INSERT INTO requests (post_id, requester_id, target_id) VALUES (?, ?, ?)',
                   (post_id, user_id, target['id']))
        db.commit()
        flash(f'You asked @{target["username"]} to join the conversation.', 'success')
    except sqlite3.IntegrityError:
        flash(f'@{target["username"]} has already been asked about this post.', 'info')

    return redirect(back)

# Design Claim 4: inbox of requests sent to the current user
@app.route('/requests')
def requests_inbox():
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see your requests.', 'danger')
        return redirect(url_for('login'))

    rows = query_db('''
        SELECT r.id, r.post_id, r.created_at, r.seen, r.answered_at,
               u.username AS requester, p.content,
               -- Design Claim 12: how many others already replied
               (SELECT COUNT(DISTINCT c.user_id) FROM comments c
                WHERE c.post_id = r.post_id AND c.user_id != ?) AS replied_count
        FROM requests r
        JOIN users u ON r.requester_id = u.id
        JOIN posts p ON r.post_id = p.id
        WHERE r.target_id = ?
        ORDER BY r.created_at DESC
    ''', (user_id, user_id))

    open_requests, answered_requests = [], []
    for row in rows:
        req = dict(row)
        req['content'], _ = moderate_content(req['content'])
        (answered_requests if req['answered_at'] else open_requests).append(req)

    # Mark as seen after loading, so "New" labels show on this visit only
    query_db('UPDATE requests SET seen = 1 WHERE target_id = ? AND seen = 0', (user_id,), commit=True)

    return render_template('requests.html.j2',
                           open_requests=open_requests,
                           answered_requests=answered_requests)

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

# Design Claim 19 (Ch. 5): welcome page with a ready-to-edit introduction post
@app.route('/welcome')
def welcome():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    user = query_db('SELECT username, location, profile FROM users WHERE id = ?', (user_id,), one=True)
    location = f" from {user['location']}" if user['location'] else ''
    intro = f"Hi everyone, I'm @{user['username']}{location}! {user['profile'] or ''} #introduction"
    return render_template('welcome.html.j2', intro=intro)

# Design Claim 19 (Ch. 5): accounts younger than 7 days are shown as new members
@app.template_global()
def is_new_member(user_id):
    user = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    return bool(user and user['created_at'] and (datetime.utcnow() - user['created_at']).days < 7)

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    # Steps 1-3: profile score and average post/comment scores (0 if none)
    profile_score = moderate_content(user['profile'])[1]
    post_scores = [moderate_content(r['content'])[1] for r in query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))]
    comment_scores = [moderate_content(r['content'])[1] for r in query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    # Step 4: weighted combination, posts count three times as much
    score = profile_score * 1 + average_post_score * 3 + average_comment_score * 1

    # Step 5: newer accounts get a higher multiplier
    account_age_days = (datetime.utcnow() - user['created_at']).days
    if account_age_days < 7:
        score *= 1.5
    elif account_age_days < 30:
        score *= 1.2

    # Step 6: cap at 5.0
    return min(score, 5.0)

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """

    if not content:
        return content, 0.0

    # Rules 1.1.1 and 1.1.2: severe violations remove the whole content
    if TIER1_RE.search(content):
        return '[content removed due to severe violation]', 5.0
    if TIER2_RE.search(content):
        return '[content removed due to spam/scam policy]', 5.0

    score = 0.0

    # Rule 1.2.1: mask each mild profanity with asterisks, +2.0 each
    moderated_content, profanity_count = TIER3_RE.subn(lambda m: '*' * len(m.group()), content)
    score += 2.0 * profanity_count

    # Rule 1.2.2: remove links, +2.0 each
    moderated_content, link_count = URL_RE.subn('[link removed]', moderated_content)
    score += 2.0 * link_count

    # Rule 1.2.3: excessive capitalization, +0.5 (text unchanged)
    if is_shouting(content):
        score += 0.5

    return moderated_content, score

# Rule 1.2.3: more than 15 letters and over 70% of them uppercase
def is_shouting(content):
    letters = [c for c in content if c.isalpha()]
    return len(letters) > 15 and sum(c.isupper() for c in letters) / len(letters) > 0.7

# Design Claim 20 (Ch. 4): readable list of the rules this text would break
def violation_reasons(content):
    if TIER1_RE.search(content):
        return ['Severe language (Rule 1.1.1): the whole text would be removed']
    if TIER2_RE.search(content):
        return ['Spam or scam phrase (Rule 1.1.2): the whole text would be removed']

    reasons = []
    profanity_count = len(TIER3_RE.findall(content))
    if profanity_count:
        reasons.append(f'Mild profanity (Rule 1.2.1): {profanity_count} word(s) would be masked')
    link_count = len(URL_RE.findall(content))
    if link_count:
        reasons.append(f'External links (Rule 1.2.2): {link_count} link(s) would be removed')
    if is_shouting(content):
        reasons.append('Excessive capital letters (Rule 1.2.3)')
    return reasons

# Design Claim 20 (Ch. 4): reminder page shown before saving text that breaks the rules
def rule_reminder(content, action):
    moderated, score = moderate_content(content)
    removed = moderated.startswith('[content removed')
    if score == 0 or (request.form.get('post_anyway') and not removed):
        return None
    back = request.form.get('back') or request.referrer or url_for('feed')
    return render_template('check_content.html.j2', content=content, moderated=moderated,
                           reasons=violation_reasons(content), removed=removed,
                           action=action, back=back)

# Coding Assignment #3

# Assignment 3.1: common words ignored when building a user's interest profile
STOP_WORDS = set('''the and are for you but not all any can her him his how its our out who why was has had
    this that with have just your from what they them their there been were will would about really today
    when then than more some into like love time make made best feel after before every something thing things
    because could should those these here very much also only being over even still sure dont cant wont youre
    thats going know need want think good great nice day new got get one'''.split())

# Assignment 3.1: meaningful words of a post (hashtags included, without the '#')
def post_words(text):
    return {w for w in re.findall(r'[a-z]+', text.lower()) if len(w) >= 3 and w not in STOP_WORDS}

# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    post_query = 'SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id FROM posts p JOIN users u ON p.user_id = u.id'

    # Posts the user reacted to positively, users they follow, and everything they already reacted to
    liked = query_db(post_query + ''' JOIN reactions r ON r.post_id = p.id
        WHERE r.user_id = ? AND r.reaction_type IN ('like', 'love', 'laugh', 'haha', 'wow')''', (user_id,))
    followed = {r['followed_id'] for r in query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}
    reacted = {r['post_id'] for r in query_db('SELECT post_id FROM reactions WHERE user_id = ?', (user_id,))}

    # Candidates: other users' posts not yet reacted to (only followed authors if filtered)
    candidates = [p for p in query_db(post_query)
                  if p['user_id'] != user_id and p['id'] not in reacted
                  and (not filter_following or p['user_id'] in followed)]

    if liked:
        # Content-based: count how often each word appears in the liked posts
        profile = collections.Counter(w for p in liked for w in post_words(p['content']))
        base_score = lambda p: sum(profile[w] for w in post_words(p['content']))
    else:
        # Cold start (e.g. a newcomer with no reactions yet): fall back to popularity
        popularity = {r['post_id']: r['cnt'] for r in query_db('SELECT post_id, COUNT(*) AS cnt FROM reactions GROUP BY post_id')}
        base_score = lambda p: popularity.get(p['id'], 0)

    # Social: posts by followed users get a bonus
    score = lambda p: base_score(p) + (3 if p['user_id'] in followed else 0)

    # Best 5 (newer wins ties), shown newest first
    top = sorted(candidates, key=lambda p: (score(p), p['created_at']), reverse=True)[:5]
    return sorted(top, key=lambda p: p['created_at'], reverse=True)

if __name__ == '__main__':
    app.run(debug=True, port=8080)

