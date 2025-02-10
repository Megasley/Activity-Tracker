import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import asyncio
import gspread
from google.oauth2.service_account import Credentials
import time
from requests.exceptions import ConnectionError
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
REPORT_CHANNEL_ID = int(os.getenv('REPORT_CHANNEL_ID'))
SHEET_ID = os.getenv('SHEET_ID')

class StatusTracker(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix='!', intents=intents)
        
        # Initialize Google Sheets connection
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credentials = Credentials.from_service_account_file('credentials.json', scopes=scopes)
        self.gclient = gspread.authorize(credentials)
        self.sheet = self.gclient.open_by_key(SHEET_ID)
        self.tracker_sheet = self.sheet.worksheet('Tracker')
        
        # Initialize headers if sheet is empty
        if not self.tracker_sheet.get_all_values():
            today = datetime.now().date().isoformat()
            self.tracker_sheet.append_row([
                'User ID', 
                'Username', 
                f'Total Minutes on {today}'
            ])
            
        # Store active sessions in memory
        self.active_sessions = {}
        self.max_retries = 3
        self.retry_delay = 2  # seconds

    def update_user_time(self, user_id, username, duration_minutes):
        """Update or create user's total time for today"""
        today = datetime.now().date().isoformat()
        
        for attempt in range(self.max_retries):
            try:
                # Get today's column
                headers = self.tracker_sheet.row_values(1)  # Get header row
                today_header = f'Total Minutes on {today}'
                
                # Find or create today's column
                if today_header not in headers:
                    next_col = len(headers) + 1
                    self.tracker_sheet.update_cell(1, next_col, today_header)
                    today_col = next_col
                else:
                    today_col = headers.index(today_header) + 1
                
                # Find or create user's row
                try:
                    cell = self.tracker_sheet.find(user_id)
                    user_row = cell.row
                    
                    # Get current value and add new minutes
                    current_value = self.tracker_sheet.cell(user_row, today_col).value
                    current_minutes = float(current_value) if current_value and current_value.strip() else 0
                    new_total = current_minutes + max(1, int(duration_minutes))  # Ensure at least 1 minute
                    
                    # Update the cell
                    self.tracker_sheet.update_cell(user_row, today_col, str(int(new_total)))
                    print(f"Updated {username}'s time: {int(new_total)} minutes (added {int(duration_minutes)})")
                    return  # Success, exit the function
                    
                except gspread.CellNotFound:
                    # Add new user row
                    row_data = [user_id, username]
                    while len(row_data) < today_col - 1:
                        row_data.append('')
                    row_data.append(str(int(max(1, duration_minutes))))
                    self.tracker_sheet.append_row(row_data)
                    print(f"Created new record for {username}: {int(duration_minutes)} minutes")
                    return  # Success, exit the function
                    
            except (ConnectionError, TimeoutError, Exception) as e:
                if attempt < self.max_retries - 1:  # Don't sleep on last attempt
                    print(f"Attempt {attempt + 1} failed, retrying in {self.retry_delay} seconds...")
                    time.sleep(self.retry_delay)
                else:
                    print(f"Final attempt failed for {username}: {str(e)}")

    def format_time(self, minutes):
        """Convert minutes to hours and minutes format"""
        hours = minutes // 60
        remaining_minutes = minutes % 60
        if hours > 0:
            return f"{int(hours)}h {int(remaining_minutes)}m"
        return f"{int(remaining_minutes)}m"

    @tasks.loop(hours=24)
    async def daily_report(self):
        if not REPORT_CHANNEL_ID:
            return
            
        channel = self.get_channel(REPORT_CHANNEL_ID)
        if not channel:
            return
        
        today = datetime.now().date().isoformat()
        all_records = self.tracker_sheet.get_all_records()
        
        # Calculate totals for each user
        user_totals = {}
        for record in all_records:
            if record['Date'] == today:
                user_id = record['User ID']
                username = record['Username']
                minutes = float(record['Total Minutes'])
                
                if user_id not in user_totals:
                    user_totals[user_id] = {'username': username, 'total': 0}
                user_totals[user_id]['total'] += minutes
        
        # Generate report
        report = "📊 **Daily Status Report**\n\n"
        for user_data in user_totals.values():
            formatted_time = self.format_time(user_data['total'])
            report += f"{user_data['username']}: {formatted_time}\n"
        
        await channel.send(report)

    async def setup_hook(self):
        print(f"Logged in as {self.user}")
        self.daily_report.start()

# Create bot instance before event handlers
bot = StatusTracker()

@bot.event
async def on_presence_update(before, after):
    print(f"Presence update detected for {after.name}")
    
    user_id = str(after.id)
    username = after.name
    current_time = datetime.now()
    
    # Track when user becomes active
    if (before.status in [discord.Status.offline, discord.Status.invisible] and 
        after.status not in [discord.Status.offline, discord.Status.invisible]):
        print(f"{username} became active")
        bot.active_sessions[user_id] = current_time
    
    # Track when user becomes inactive
    elif (before.status not in [discord.Status.offline, discord.Status.invisible] and 
          after.status in [discord.Status.offline, discord.Status.invisible]):
        print(f"{username} became inactive")
        if user_id in bot.active_sessions:
            start_time = bot.active_sessions[user_id]
            duration_minutes = (current_time - start_time).total_seconds() / 60
            bot.update_user_time(user_id, username, duration_minutes)
            del bot.active_sessions[user_id]

@bot.command()
async def mystatus(ctx):
    """Command to check your current status statistics"""
    user_id = str(ctx.author.id)
    today = datetime.now().date().isoformat()
    
    try:
        # Get today's column
        headers = bot.tracker_sheet.row_values(1)
        today_header = f'Total Minutes on {today}'
        
        if today_header not in headers:
            await ctx.send("No activity recorded today!")
            return
            
        today_col = headers.index(today_header) + 1
        
        # Find user's row
        try:
            cell = bot.tracker_sheet.find(user_id)
            user_row = cell.row
            
            # Get current total
            current_value = bot.tracker_sheet.cell(user_row, today_col).value
            total_minutes = float(current_value) if current_value else 0
            
            # Add current session if user is active
            if user_id in bot.active_sessions:
                current_session_minutes = (datetime.now() - bot.active_sessions[user_id]).total_seconds() / 60
                total_minutes += current_session_minutes
            
            formatted_time = bot.format_time(total_minutes)
            await ctx.send(f"You've been online for {formatted_time} today!")
            
        except gspread.CellNotFound:
            await ctx.send("No activity recorded yet!")
            
    except Exception as e:
        print(f"Error in mystatus: {e}")
        await ctx.send("Error getting status!")

@bot.command()
async def testreport(ctx):
    """Generate a status report immediately"""
    today = datetime.now().date().isoformat()
    
    try:
        # Get today's column
        headers = bot.tracker_sheet.row_values(1)
        today_header = f'Total Minutes on {today}'
        
        if today_header not in headers:
            await ctx.send("No activity recorded today!")
            return
            
        today_col = headers.index(today_header) + 1
        
        # Get all rows
        all_rows = bot.tracker_sheet.get_all_values()
        
        report = "📊 **Current Status Report**\n\n"
        
        # Skip header row
        for row in all_rows[1:]:
            if len(row) >= today_col:  # Make sure row has today's column
                user_id = row[0]
                username = row[1]
                minutes = float(row[today_col - 1]) if row[today_col - 1] else 0
                
                # Add current session if user is active
                if user_id in bot.active_sessions:
                    current_session = (datetime.now() - bot.active_sessions[user_id]).total_seconds() / 60
                    minutes += current_session
                
                if minutes > 0:  # Only show users with activity
                    formatted_time = bot.format_time(minutes)
                    report += f"{username}: {formatted_time}\n"
        
        await ctx.send(report)
        
    except Exception as e:
        print(f"Error in testreport: {e}")
        await ctx.send("Error generating report!")

@bot.command()
async def sessions(ctx):
    """Show all sessions for the user"""
    user_id = str(ctx.author.id)
    
    if user_id not in bot.user_status:
        await ctx.send("No status data available for you yet!")
        return
    
    sessions = bot.user_status[user_id]['sessions']
    if not sessions:
        await ctx.send("No sessions recorded yet!")
        return
    
    report = "**Your sessions today:**\n\n"
    today = datetime.now().date()
    
    for session in sessions:
        start = datetime.fromisoformat(session['start'])
        if start.date() == today:
            end = datetime.fromisoformat(session['end']) if session['end'] else "Still online"
            report += f"Start: {start.strftime('%H:%M:%S')}, End: {end}\n"
    
    await ctx.send(report)

@bot.command()
async def status_debug(ctx):
    """Debug command to show current status"""
    member = ctx.author
    await ctx.send(f"""
Status Debug for {member.name}:
Current Status: {member.status}
Mobile Status: {member.mobile_status}
Desktop Status: {member.desktop_status}
Web Status: {member.web_status}
Raw Status: {member.raw_status}
""")

# Run the bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
